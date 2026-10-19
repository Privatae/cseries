#!/usr/bin/env python3
"""NAC ingress rules say what they admit, never raise, and name who approved them.

The defects pinned here:

  1. An approved ingress ALLOW with no permits DENIED every machine it matched:
     permits_inbound grants an allow exactly its permits, propose_policy defaulted
     them to [], the tool schema made them optional, and the Ingress card never
     showed them. The engine's meaning is kept (an allow still grants only what it
     names — nothing is loosened); instead an allow that names no lane can no longer
     be drafted or approved, and the card shows what each rule admits.
  2. The tool schema typed permits as STRINGS; permits_inbound then raised on a
     string, and _nac_gate (server/routes/mcp.py) admits on any engine exception,
     so a malformed rule admitted by accident. Strings are refused at propose and
     in the schema, and permits_inbound skips a malformed permit instead of
     raising. The gate's admit-on-error is deliberately UNCHANGED (a separate decision) —
     the last block pins that so it cannot flip silently either way.
  3. approve_policy / approve_egress / approve_mgmt_grant wrote approved_by='user'
     for the host tool approve_nac_draft, which makes a tightening draft live with
     no user involved. The host is now recorded as 'host'.

  4. Pairing approvals AND refusals from the host (manage_pairing approve / accept / deny / decline) were
     recorded as the user too; they follow the same rule now.
  5. Revoking an approved ingress QUARANTINE was free, so the host could undo the
     user's quarantine on its own. It now asks for the code, like removing an
     approved egress deny; removing an allow and declining a draft stay free.

  6. An approved ALLOW that admits nothing on mcp (no permits, legacy strings, or a
     row failing its integrity check) is keeping machines out too, so removing it
     asks for the code. The decision is the engine's own reading of the row.
  7. The decline/approve race: a decline only removes a row that is still a draft;
     one approved since the read is decided again as approved (policy and egress).
  8. A re-draft may not rewrite the profile an approved rule points to — a legacy
     unsigned rule is read from it, so a free re-draft could change what it enforces.

Pure: fake pools, stubbed signing, no DB, no network. The card check runs the
real app.js functions under node.

Run: APP_ROOT=<repo> python3 scripts/test_nac_allow_permits.py
"""
import ast
import asyncio
import json
import os
import subprocess
import sys

ROOT = os.environ.get("APP_ROOT") or os.environ.get("CSERIES_ROOT") or \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]

from server.network import nac_commit, nac_propose, nac_stepup   # noqa: E402
from server.network.nac_controller import NACController          # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{extra}]" if not cond and extra else ""))
    if not cond:
        _fails.append(name)


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc as e:
        return e
    except Exception as e:  # the wrong exception is a failure, reported with its type
        return AssertionError(f"raised {type(e).__name__}: {e}")
    return None


# ── fakes ────────────────────────────────────────────────────────────────────
class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    """`rows` scripts successive fetchrow answers (the last repeats) and `updates` scripts
    successive execute() answers — how the tests stage a draft approved between a read and
    a write. `approved_under_name` answers propose_policy's "is an approved rule already
    using this name" lookup."""
    def __init__(self, row, rows=None, updates=None, approved_under_name=None):
        self.row = row
        self.rows = list(rows) if rows else None
        self.updates = list(updates) if updates else []
        self.approved_under_name = approved_under_name
        self.executed = []
        self.applied = []      # executes that changed a row
        self.inserted = []
        self.reads = []        # every fetchrow query, as sent

    async def fetchrow(self, q, *a):
        self.reads.append((q, a))
        if self.rows:
            return self.rows.pop(0) if len(self.rows) > 1 else self.rows[0]
        return self.row

    async def fetchval(self, q, *a):
        if "approved_at IS NOT NULL" in q:
            return self.approved_under_name
        self.inserted.append((q, a))
        return 11

    async def execute(self, q, *a):
        self.executed.append((q, a))
        status = self.updates.pop(0) if self.updates else "UPDATE 1"
        if not status.endswith(" 0"):
            self.applied.append((q, a))
        return status

    def transaction(self):
        return _Tx()


class _Acq:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, row=None, **kw):
        self.conn = _Conn(row, **kw)
        self.acquired = 0

    def acquire(self):
        self.acquired += 1
        return _Acq(self.conn)

    async def execute(self, q, *a):   # pool-level writes land on the same record
        return await self.conn.execute(q, *a)

    async def fetchrow(self, q, *a):  # pool-level reads see the same rows (asyncpg has both)
        return await self.conn.fetchrow(q, *a)


_stepup_calls = []


async def _verify_ok(pool, code):
    _stepup_calls.append(code)
    return True


async def _sign(pool, eid, terms):
    return "entity_sign", "sig"


nac_stepup.verify_stepup = _verify_ok
nac_stepup.sign_policy_terms = _sign


def _policy_row(enforcement, permits):
    return {"id": 7, "name": "r", "priority": 100, "match": "{}", "enforcement": enforcement,
            "permits": json.dumps(permits), "approved_at": None}


def _run(coro_fn, pool):
    """Run an approve; a refusal or a missing keyword is returned, never raised, so
    one broken path reports as a FAIL instead of hiding every check after it."""
    try:
        return pool, asyncio.run(coro_fn())
    except (nac_commit.CommitError, TypeError) as e:
        return pool, e


def _approve_policy(row, **kw):
    pool = _Pool(row)
    return _run(lambda: nac_commit.approve_policy(pool, 3, 7, stepup_code="123456", **kw), pool)


def _approver_of(pool):
    """approved_by as written by the UPDATE (second bind parameter), or None."""
    return pool.conn.executed[0][1][1] if pool.conn.executed else None


# ── 1. an allow must say what it admits ──────────────────────────────────────
print("1. an allow rule cannot be drafted or approved without naming what it admits")
for bad in (None, []):
    pool = _Pool()
    e = raises(lambda: asyncio.run(nac_propose.propose_policy(
        pool, name="x", enforcement="allow", permits=bad)))
    check(f"propose allow with permits={bad!r} is refused", isinstance(e, ValueError), repr(e))
    check(f"...before anything is written (permits={bad!r})", pool.acquired == 0)

pool = _Pool()
res = asyncio.run(nac_propose.propose_policy(pool, name="x", enforcement="allow",
                                             permits=[{"lane": "mcp"}]))
stored = json.loads(pool.conn.inserted[0][1][2]) if pool.conn.inserted else None
check("propose allow with a lane is drafted", res.get("status") == "draft")
check("the stored permit is written explicitly as inbound",
      stored == [{"direction": "inbound", "lane": "mcp"}], stored)

for bad, why in (([{"lane": "harness"}], "a lane the engine never evaluates"),
                 ([{"lane": "mcp", "ports": "22"}], "a key the engine does not enforce"),
                 ([{"direction": "outbound", "lane": "mcp"}], "an outbound permit"),
                 ([{}], "a permit that names no lane")):
    e = raises(lambda: asyncio.run(nac_propose.propose_policy(
        _Pool(), name="x", enforcement="allow", permits=bad)))
    check(f"propose refuses {why}", isinstance(e, ValueError), repr(e))
e = raises(lambda: asyncio.run(nac_propose.propose_policy(
    _Pool(), name="q", enforcement="quarantine", permits=[{"lane": "mcp"}])))
check("propose refuses permits on a quarantine (it denies every lane)", isinstance(e, ValueError), repr(e))
res = asyncio.run(nac_propose.propose_policy(_Pool(), name="q", enforcement="quarantine"))
check("a quarantine with no permits is still drafted", res.get("status") == "draft")

_stepup_calls.clear()
pool, out = _approve_policy(_policy_row("allow", []), via="ui")
check("approving an allow draft with no permits is refused 400",
      isinstance(out, nac_commit.CommitError) and out.status == 400, repr(out))
check("...before the step-up code is checked, so the one-time code is not burned",
      _stepup_calls == [], _stepup_calls)
check("...and nothing is signed or written", pool.conn.executed == [])

_stepup_calls.clear()
pool, out = _approve_policy(_policy_row("allow", [{"lane": "any"}]), via="ui")
signed = json.loads(pool.conn.executed[0][1][4]) if pool.conn.executed else {}
check("an allow with a lane approves, with the step-up checked",
      isinstance(out, dict) and out.get("approved") and _stepup_calls == ["123456"], repr(out))
check("the signed terms carry the explicit permit",
      signed.get("permits") == [{"direction": "inbound", "lane": "any"}], signed)

nac = NACController(verify_pubkey=None)
check("the engine is not loosened: an allow with no permits still admits nothing",
      nac.permits_inbound({"enforcement": "allow", "permits": []}) is False)
check("an allow naming the mcp lane admits on it",
      nac.permits_inbound({"enforcement": "allow",
                           "permits": [{"direction": "inbound", "lane": "mcp"}]}) is True)


# policy_list: an approved rule is shown from its SIGNED terms, a draft from its profile.
class _ListPool:
    async def fetch(self, q, *a):
        if "FROM nac_profiles" in q:
            # A later same-name draft rewrote the shared profile to a quarantine.
            return [{"id": 5, "name": "r", "enforcement": "quarantine", "is_default": False,
                     "permits": "[]"}]
        return [
            {"id": 1, "name": "r", "priority": 10, "match": "{}", "profile_id": 5,
             "proposed_by": "host", "approved_at": "2026-09-27T00:00:00Z", "approved_by": "user",
             "stepup_at": None,
             "signed_terms": json.dumps({"enforcement": "allow",
                                         "permits": [{"direction": "inbound", "lane": "mcp"}]})},
            {"id": 2, "name": "r", "priority": 10, "match": "{}", "profile_id": 5,
             "proposed_by": "host", "approved_at": None, "approved_by": None, "stepup_at": None,
             "signed_terms": None},
        ]


async def _not_enrolled(pool):
    return False


nac_stepup.is_enrolled = _not_enrolled
listed = asyncio.run(nac_commit.policy_list(_ListPool(), 3))["policy"]
check("policy_list: an approved rule shows its signed enforcement + permits",
      listed[0].get("enforcement") == "allow"
      and listed[0].get("permits") == [{"direction": "inbound", "lane": "mcp"}], listed[0])
check("policy_list: a draft shows its profile's enforcement + permits",
      listed[1].get("enforcement") == "quarantine" and listed[1].get("permits") == [], listed[1])
check("policy_list does not send the raw signed terms", "signed_terms" not in listed[0])


# The card, running the real app.js functions: every rule says what it admits.
def _card_summaries():
    src = open(os.path.join(ROOT, "system-apps/network/app.js"), encoding="utf-8").read()
    start = src.find("function policyEnforcement(r)")
    end = src.find("// Second-factor chip")
    if start < 0 or end < start:
        return None, "could not locate policyEnforcement..admitsSummary in app.js"
    js = ("const state = {nac: {profiles: [{id: 9, enforcement: 'allow', permits: '[]'}]}};\n"
          + src[start:end] + "\n"
          "console.log(JSON.stringify([\n"
          "  admitsSummary({enforcement: 'allow', permits: [{direction: 'inbound', lane: 'mcp'}]}),\n"
          "  admitsSummary({enforcement: 'allow', permits: [{lane: 'any'}]}),\n"
          "  admitsSummary({enforcement: 'allow', permits: []}),\n"
          "  admitsSummary({enforcement: 'allow', permits: ['mcp']}),\n"
          "  admitsSummary({profile_id: 9}),\n"
          "  admitsSummary({enforcement: 'quarantine', permits: []}),\n"
          "]));\n")
    try:
        p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None, "node is not installed; the card check cannot run"
    if p.returncode != 0:
        return None, p.stderr.strip()[-300:]
    return json.loads(p.stdout), ""


cards, err = _card_summaries()
check("the card's summary functions run under node", cards is not None, err)
if cards:
    check("card: an mcp allow says it admits paired machines", cards[0] == "admits paired machines (mcp)", cards[0])
    check("card: an 'any' allow says every lane", cards[1] == "admits every lane", cards[1])
    check("card: an allow with no permits says it locks machines out",
          "locked out" in cards[2], cards[2])
    check("card: an allow with only a string permit also says it locks machines out",
          "locked out" in cards[3], cards[3])
    check("card: an older read (profile only) still gets a summary", "locked out" in cards[4], cards[4])
    check("card: a quarantine admits nothing", cards[5] == "admits nothing", cards[5])


# ── 2. a string permit is refused and never raises in the engine ─────────────
print("2. a string permit is refused at propose, typed out of the schema, and never raises")
e = raises(lambda: asyncio.run(nac_propose.propose_policy(
    _Pool(), name="x", enforcement="allow", permits=["mcp"])))
check("propose refuses a string permit", isinstance(e, ValueError), repr(e))
e = raises(lambda: asyncio.run(nac_propose.propose_policy(
    _Pool(), name="x", enforcement="allow", permits="mcp")))
check("propose refuses permits given as a bare string", isinstance(e, ValueError), repr(e))
_stepup_calls.clear()
pool, out = _approve_policy(_policy_row("allow", ["mcp"]), via="ui")
check("approving a stored allow with a string permit is refused 400, code unburned",
      isinstance(out, nac_commit.CommitError) and out.status == 400 and _stepup_calls == [], repr(out))

def _inbound(permits):
    """permits_inbound's answer for an allow, or the exception it raised."""
    try:
        return nac.permits_inbound({"enforcement": "allow", "permits": permits})
    except Exception as e:
        return e


for permits in (["mcp"], "mcp", {"lane": "mcp"}, [None, 3, ["mcp"]]):
    got = _inbound(permits)
    check(f"permits_inbound does not raise on permits={permits!r}",
          not isinstance(got, Exception), repr(got))
    check(f"...and a malformed permit admits nothing ({permits!r})", got is False, repr(got))
got = _inbound(["junk", {"direction": "inbound", "lane": "mcp"}])
check("a valid permit after a malformed one still admits", got is True, repr(got))

from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS   # noqa: E402
from server.network import nac_controller as _ctl                 # noqa: E402

PERMIT_LANES = getattr(_ctl, "PERMIT_LANES", None)
normalize_permits = getattr(_ctl, "normalize_permits", None)
check("the engine owns the permit vocabulary (PERMIT_LANES + normalize_permits)",
      PERMIT_LANES is not None and normalize_permits is not None)
pp = PUBLIC_TOOL_SCHEMAS["propose_nac_policy"]["function"]["parameters"]["properties"]["permits"]
items = pp.get("items") or {}
check("schema: a permit is an object, not a string", items.get("type") == "object", items)
lane_enum = ((items.get("properties") or {}).get("lane") or {}).get("enum")
check("schema: the lane enum is exactly the engine's lanes",
      PERMIT_LANES is not None and lane_enum == list(PERMIT_LANES), lane_enum)
check("schema: a permit must name its lane", "lane" in (items.get("required") or []))
check("schema: every lane it offers is accepted by the validator",
      normalize_permits is not None and bool(lane_enum)
      and all(normalize_permits("allow", [{"lane": ln}]) for ln in lane_enum))

# End to end through the real gate: a stored allow whose permits are strings used to
# raise inside evaluate() and be ADMITTED by the gate's admit-on-error. It now denies.
from server.routes.mcp import _nac_gate   # noqa: E402

_PROFILES = [{"id": 1, "name": "str-allow", "enforcement": "allow", "is_default": False,
              "permits": ["mcp"]},
             {"id": 2, "name": "ok-allow", "enforcement": "allow", "is_default": False,
              "permits": [{"direction": "inbound", "lane": "mcp"}]}]


def _nac_with(profile_id):
    n = NACController(verify_pubkey=None)
    n._ingest(_PROFILES, [{"id": 1, "name": "r", "priority": 1, "match": {},
                           "profile_id": profile_id, "signed_terms": None, "signature": None}])
    return n


device = {"id": 5, "hardware_fingerprint": "FP", "entity_id": 9003}
decision = asyncio.run(_nac_gate(device, nac=_nac_with(1)))
check("gate: an allow with only string permits is DENIED, not admitted by an exception",
      decision is not None and decision.get("allowed") is False, repr(decision))
check("gate: an allow naming the mcp lane is admitted",
      asyncio.run(_nac_gate(device, nac=_nac_with(2))) is None)


class _Broken:
    async def evaluate(self, token_id):
        raise RuntimeError("engine error")


# Deliberately unchanged: whether an engine error admits or refuses is a separate
# decision. This pins the current behaviour so that neither direction changes quietly.
check("gate: an engine error still admits (fail-open unchanged, pending its own decision)",
      asyncio.run(_nac_gate(device, nac=_Broken())) is None)


# ── 3. the host is recorded as the host ─────────────────────────────────────
print("3. approved_by names who committed the rule")
pool, out = _approve_policy(_policy_row("quarantine", []), via="host_tool")
check("host tool approves a quarantine: recorded 'host'", _approver_of(pool) == "host", repr(out))
check("...and the result says so", isinstance(out, dict) and out.get("approved_by") == "host", repr(out))
pool, out = _approve_policy(_policy_row("allow", [{"lane": "mcp"}]), via="host_tool")
check("host tool approves an allow with a verified code: still 'host' (a verified code does "
      "not prove the user typed it); stepup_at records the code",
      _approver_of(pool) == "host" and pool.conn.executed[0][1][2] is True, repr(out))
pool, out = _approve_policy(_policy_row("quarantine", []))
check("a caller that names no surface is recorded 'host', never the user",
      _approver_of(pool) == "host", _approver_of(pool))
pool, out = _approve_policy(_policy_row("quarantine", []), via="ui", approved_by="alex@example.com")
check("the Network app records the signed-in user", _approver_of(pool) == "alex@example.com", repr(out))
pool, out = _approve_policy(_policy_row("quarantine", []), via="ui")
check("the Network app with no identity header records 'user'", _approver_of(pool) == "user", repr(out))
pool, out = _approve_policy(_policy_row("quarantine", []), via="host_tool", approved_by="alex@example.com")
check("a non-UI caller cannot claim to be the user", _approver_of(pool) == "host", repr(out))

pool = _Pool({"id": 4, "pattern": "evil.example", "action": "deny", "priority": 100,
              "subject": "*", "exit": "default", "approved_at": None})
pool, out = _run(lambda: nac_commit.approve_egress(pool, 3, 4, via="host_tool"), pool)
check("host tool approves an egress DENY: recorded 'host'", _approver_of(pool) == "host", repr(out))
pool = _Pool({"id": 6, "server": "box1", "ops": "[]", "deny": '["contain"]', "intent": None,
              "approved_at": None})
pool, out = _run(lambda: nac_commit.approve_mgmt_grant(pool, 3, 6, via="host_tool"), pool)
check("host tool approves a deny-only mgmt grant: recorded 'host'", _approver_of(pool) == "host", repr(out))

# The host tool names itself for every kind.
from mastercode.runtime import tools   # noqa: E402
from server import brain_service        # noqa: E402

seen = {}


def _capture(kind):
    async def f(pool, eid, rid, **kw):
        seen[kind] = kw.get("via")
        return {"approved": True}
    return f


_orig = (nac_commit.approve_policy, nac_commit.approve_egress, nac_commit.approve_mgmt_grant,
         brain_service.get_pool)
nac_commit.approve_policy = _capture("policy")
nac_commit.approve_egress = _capture("egress")
nac_commit.approve_mgmt_grant = _capture("mgmt_grant")
brain_service.get_pool = lambda: object()
try:
    for kind in ("policy", "egress", "mgmt_grant"):
        asyncio.run(tools._exec_approve_nac_draft({"kind": kind, "id": 1}))
finally:
    (nac_commit.approve_policy, nac_commit.approve_egress, nac_commit.approve_mgmt_grant,
     brain_service.get_pool) = _orig
check("approve_nac_draft passes via='host_tool' for policy, egress and mgmt_grant",
      seen == {"policy": "host_tool", "egress": "host_tool", "mgmt_grant": "host_tool"}, seen)

# The Network app's three approve routes each name their surface.
tree = ast.parse(open(os.path.join(ROOT, "server/routes/network.py"), encoding="utf-8").read())
vias = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "nac_commit" \
            and node.func.attr in ("approve_policy", "approve_egress", "approve_mgmt_grant"):
        kw = {k.arg: k.value for k in node.keywords}
        v = kw.get("via")
        vias.setdefault(node.func.attr, []).append(v.value if isinstance(v, ast.Constant) else None)
check("the Network app's approve routes pass via='ui' (policy, egress, mgmt grant)",
      vias == {"approve_policy": ["ui"], "approve_egress": ["ui"], "approve_mgmt_grant": ["ui"]}, vias)


def _route_kwargs(module_attr, func):
    """Keyword names/constant values on every call `<module_attr>.<func>(...)` in the routes."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == module_attr \
                and node.func.attr == func:
            out.append({k.arg: (k.value.value if isinstance(k.value, ast.Constant) else "<expr>")
                        for k in node.keywords})
    return out


class _Patch:
    """Set module attributes for one block and put them back after."""
    def __init__(self, *triples):
        self.triples = triples
        self.saved = []

    def __enter__(self):
        for mod, name, val in self.triples:
            self.saved.append((mod, name, getattr(mod, name)))
            setattr(mod, name, val)
        return self

    def __exit__(self, *a):
        for mod, name, val in reversed(self.saved):
            setattr(mod, name, val)
        return False


# ── 4. pairing approvals name who committed them ─────────────────────────────
print("4. a pairing approved by the host is recorded as the host")
from server.network import pairing_grants as pg, machine_offers as mo   # noqa: E402

granted = []


async def _grant_capture(pool, eid, server, subject, handle, caps, models, **kw):
    granted.append(kw.get("approved_by"))
    return {"approved": True, "delivered": True, "expires": "", "note": None}


async def _anchored(pool, eid, server):
    return {"token_id": 1, "pairings": [{"host_name": "peer.example", "handle": "peer"}]}


async def _stepup_true(pool, code):
    return True


def _approve_pairing(**kw):
    granted.clear()
    with _Patch((pg, "_anchored_here", _anchored), (pg, "_verify_stepup", _stepup_true),
                (pg, "sign_and_push_grant", _grant_capture)):
        try:
            asyncio.run(pg.approve_pairing(object(), 3, "box1", "peer.example", ["observe"],
                                           stepup_code="123456", **kw))
        except TypeError as e:
            return e
    return granted[0] if granted else None


check("manage_pairing approve (via='host_tool') is recorded 'host'",
      _approve_pairing(via="host_tool") == "host", _approve_pairing(via="host_tool"))
check("a pairing approval that names no surface is recorded 'host', never the user",
      _approve_pairing() == "host", _approve_pairing())
check("the Network app's pairing approval records the signed-in user",
      _approve_pairing(via="ui", approved_by="alex@example.com") == "alex@example.com")
check("the Network app's pairing approval with no identity header records 'user'",
      _approve_pairing(via="ui") == "user", _approve_pairing(via="ui"))


class _OfferPool:
    def __init__(self):
        self.executed = []

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "UPDATE 1"


async def _open_offer(pool, offer_id, direction):
    return {"id": 9, "peer_entity_id": 44, "state": "open"}


async def _noop(*a, **k):
    return None


async def _token(peer, **k):
    return "tok"


async def _redeem(pool, eid, server, token):
    return {"subject": "peer.example", "handle": "peer"}


def _accept(**kw):
    """(approved_by on the grant, decided_by on the offer row) for one accept."""
    granted.clear()
    pool = _OfferPool()
    with _Patch((mo, "_open_offer", _open_offer), (mo, "request_token_from_peer", _token),
                (mo, "_archive_cards", _noop), (mo, "_tell_them", _noop),
                (pg, "_anchored_here", _anchored), (pg, "_verify_stepup", _stepup_true),
                (pg, "_refuse_scope_the_box_cannot_enforce", lambda row, models: None),
                (pg, "redeem_on_box", _redeem), (pg, "sign_and_push_grant", _grant_capture)):
        try:
            asyncio.run(mo.accept_inbound(pool, 3, 9, server="box1", caps=["inference"],
                                          models=["llama3"], stepup_code="123456", **kw))
        except TypeError as e:
            return e, None
    offer_row = [a for q, a in pool.executed if "network_machine_offers" in q]
    return (granted[0] if granted else None), (offer_row[0][1] if offer_row else None)


check("manage_pairing accept (via='host_tool') records 'host' on the grant and the offer",
      _accept(via="host_tool") == ("host", "host"), _accept(via="host_tool"))
check("an accept that names no surface records 'host'", _accept() == ("host", "host"), _accept())
check("the sharing card's accept records the signed-in user on both",
      _accept(via="ui", decided_by="alex@example.com") == ("alex@example.com", "alex@example.com"),
      _accept(via="ui", decided_by="alex@example.com"))

pairing_vias = {}


def _pairing_capture(name):
    async def f(*a, **kw):
        pairing_vias[name] = kw.get("via")
        return {"ok": True}
    return f


with _Patch((pg, "approve_pairing", _pairing_capture("approve")),
            (mo, "accept_inbound", _pairing_capture("accept")),
            (brain_service, "get_pool", lambda: object())):
    asyncio.run(tools._exec_manage_pairing({"op": "approve", "server": "box1", "subject": "peer.example",
                                            "caps": ["observe"], "stepup_code": "123456"}))
    asyncio.run(tools._exec_manage_pairing({"op": "accept", "offer_id": 9, "server": "box1",
                                            "models": ["llama3"], "stepup_code": "123456"}))
check("manage_pairing passes via='host_tool' for approve and accept",
      pairing_vias == {"approve": "host_tool", "accept": "host_tool"}, pairing_vias)
pa = _route_kwargs("pairing_grants", "approve_pairing")
ma = _route_kwargs("machine_offers", "accept_inbound")
check("the pairing approve route and the sharing-card accept route pass via='ui'",
      [k.get("via") for k in pa] == ["ui"] and [k.get("via") for k in ma] == ["ui"], (pa, ma))

# Refusals follow the same rule: a deny or decline the host made is the host's, or the
# audit trail says the person refused something they never saw.
from server.network import nac_mgmt   # noqa: E402


async def _bastion(pool, server):
    return {"token_id": 7}


def _deny(**kw):
    """denied_by as written for one deny_pairing, or the exception."""
    pool = _OfferPool()
    with _Patch((pg, "_bastion_row", _bastion), (nac_mgmt, "audit_mgmt", _noop)):
        try:
            asyncio.run(pg.deny_pairing(pool, 3, "box1", "peer.example", **kw))
        except TypeError as e:
            return e
    rows = [a for q, a in pool.executed if "network_bastion_pairing" in q]
    return rows[0][2] if rows else None


def _decline(**kw):
    """decided_by as written for one decline_inbound, or the exception."""
    pool = _OfferPool()
    with _Patch((mo, "_open_offer", _open_offer), (mo, "_archive_cards", _noop)):
        try:
            asyncio.run(mo.decline_inbound(pool, 9, **kw))
        except TypeError as e:
            return e
    rows = [a for q, a in pool.executed if "network_machine_offers" in q]
    return rows[0][1] if rows else None


check("manage_pairing deny (via='host_tool') is recorded 'host'",
      _deny(via="host_tool") == "host", repr(_deny(via="host_tool")))
check("a deny that names no surface is recorded 'host', never the user", _deny() == "host", repr(_deny()))
check("the Network app's deny records the signed-in user",
      _deny(via="ui", denied_by="alex@example.com") == "alex@example.com",
      repr(_deny(via="ui", denied_by="alex@example.com")))
check("the Network app's deny with no identity header records 'user'",
      _deny(via="ui") == "user", repr(_deny(via="ui")))
check("manage_pairing decline (via='host_tool') is recorded 'host'",
      _decline(via="host_tool") == "host", repr(_decline(via="host_tool")))
check("a decline that names no surface is recorded 'host', never the user",
      _decline() == "host", repr(_decline()))
check("the sharing card's decline records the signed-in user",
      _decline(via="ui", decided_by="alex@example.com") == "alex@example.com",
      repr(_decline(via="ui", decided_by="alex@example.com")))

refusal_vias = {}


def _refusal_capture(name):
    async def f(*a, **kw):
        refusal_vias[name] = kw.get("via")
        return {"ok": True}
    return f


with _Patch((pg, "deny_pairing", _refusal_capture("deny")),
            (mo, "decline_inbound", _refusal_capture("decline")),
            (brain_service, "get_pool", lambda: object())):
    asyncio.run(tools._exec_manage_pairing({"op": "deny", "server": "box1", "subject": "peer.example"}))
    asyncio.run(tools._exec_manage_pairing({"op": "decline", "offer_id": 9}))
check("manage_pairing passes via='host_tool' for deny and decline",
      refusal_vias == {"deny": "host_tool", "decline": "host_tool"}, refusal_vias)
pd = _route_kwargs("pairing_grants", "deny_pairing")
md = _route_kwargs("machine_offers", "decline_inbound")
check("the pairing deny route and the sharing-card decline route pass via='ui'",
      [k.get("via") for k in pd] == ["ui"] and [k.get("via") for k in md] == ["ui"], (pd, md))


# ── 5–8. removing a rule that keeps machines out asks for the code ───────────
print("5. removing an approved rule that keeps machines out is a widen and needs the code")
_codes = []


async def _verify_code(pool, code):
    _codes.append(code)
    return code == "123456"


import base64   # noqa: E402

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey   # noqa: E402
from server.network.nac_controller import _canonical   # noqa: E402

_rk = Ed25519PrivateKey.generate()         # an ephemeral stand-in for the entity key
_RPUB = _rk.public_key().public_bytes_raw()
_APPROVED = "2026-09-27T00:00:00Z"
_MCP = [{"direction": "inbound", "lane": "mcp"}]


def _prow(enforcement, permits, *, approved=True, signed=True, tamper=False,
          prof_enforcement=None, prof_permits=None):
    """A policy row as revoke_policy reads it, with genuinely signed terms."""
    terms = {"name": "r", "priority": 10, "match": {}, "enforcement": enforcement, "permits": permits}
    sig = base64.b64encode(_rk.sign(_canonical(terms))).decode()
    if tamper:   # stored terms changed after signing: the signature no longer verifies
        terms = dict(terms, permits=[{"direction": "inbound", "lane": "any"}])
    on = approved and signed
    return {"id": 7, "name": "r", "priority": 10, "match": "{}", "profile_id": 5,
            "approved_at": _APPROVED if approved else None,
            "signed_terms": json.dumps(terms) if on else None, "signature": sig if on else None,
            "prof_id": 5, "prof_name": "r",
            "prof_enforcement": prof_enforcement or enforcement,
            "prof_permits": json.dumps(permits if prof_permits is None else prof_permits),
            # the previous revoke_policy's column name, so it reports FAILs instead of crashing
            "enforcement": prof_enforcement or enforcement}


async def _entity_pub(pool, eid):
    return _RPUB


async def _no_key(pool, eid):
    raise RuntimeError("entity key unavailable")


def _revoke(row, key=_entity_pub, **kw):
    pool = _Pool(row) if not isinstance(row, dict) or "rows" not in row else _Pool(**row)
    with _Patch((nac_stepup, "verify_stepup", _verify_code), (nac_stepup, "entity_verify_pubkey", key)):
        _codes.clear()
        pool, out = _run(lambda: nac_commit.revoke_policy(pool, 3, 7, **kw), pool)
    revoked = any("revoked_at=now()" in q for q, _a in pool.conn.applied)
    return out, revoked, list(_codes)


def _needs_code(row, **kw):
    """(refused without a code, removed with it)."""
    out, revoked, _ = _revoke(row, **kw)
    refused = isinstance(out, nac_commit.CommitError) and out.status == 403 and not revoked
    out2, revoked2, codes2 = _revoke(row, stepup_code="123456", **kw)
    return refused, bool(isinstance(out2, dict) and revoked2 and codes2 == ["123456"])


out, revoked, codes = _revoke(_prow("quarantine", []))
check("revoking an approved quarantine WITHOUT a code is refused 403",
      isinstance(out, nac_commit.CommitError) and out.status == 403, repr(out))
check("...and the quarantine stays in force", revoked is False)
out, revoked, codes = _revoke(_prow("quarantine", []), stepup_code="000000")
check("revoking an approved quarantine with a WRONG code is refused",
      isinstance(out, nac_commit.CommitError) and out.status == 403 and not revoked, repr(out))
out, revoked, codes = _revoke(_prow("quarantine", []), stepup_code="123456")
check("revoking an approved quarantine WITH the code succeeds",
      isinstance(out, dict) and out.get("revoked") and revoked and codes == ["123456"], repr(out))
out, revoked, codes = _revoke(_prow("allow", _MCP))
check("revoking an approved allow that admits needs no code (a tighten)",
      isinstance(out, dict) and revoked and codes == [], (repr(out), codes))
out, revoked, codes = _revoke(_prow("quarantine", [], approved=False))
check("declining a quarantine DRAFT needs no code (it was never in force)",
      isinstance(out, dict) and revoked and codes == [], (repr(out), codes))
out, revoked, codes = _revoke(_prow("quarantine", [], prof_enforcement="allow", prof_permits=_MCP))
check("the signed terms decide, not the shared profile: a quarantine whose profile a later "
      "draft rewrote to 'allow' still needs the code",
      isinstance(out, nac_commit.CommitError) and out.status == 403 and not revoked, repr(out))
out, revoked, codes = _revoke(None)
check("revoking a rule that does not exist is a 404", isinstance(out, nac_commit.CommitError)
      and out.status == 404, repr(out))

# An ALLOW can keep machines out too: the engine denies mcp when its permits name no lane
# it checks, and removing it drops those machines to default-preserve, which admits.
check("revoking an approved allow with NO permits needs the code (it denies mcp today)",
      _needs_code(_prow("allow", [])) == (True, True), _needs_code(_prow("allow", [])))
check("revoking an approved allow with legacy STRING permits needs the code",
      _needs_code(_prow("allow", ["mcp"])) == (True, True), _needs_code(_prow("allow", ["mcp"])))
check("a legacy UNSIGNED approved allow with no permits (read from its profile) needs the code",
      _needs_code(_prow("allow", [], signed=False)) == (True, True))
out, revoked, codes = _revoke(_prow("allow", _MCP, signed=False))
check("a legacy UNSIGNED approved allow that admits (read from its profile) is free",
      isinstance(out, dict) and revoked and codes == [], (repr(out), codes))
check("revoking a row whose signature no longer verifies needs the code (the engine fails the "
      "whole mesh closed on it)", _needs_code(_prow("allow", [], tamper=True)) == (True, True),
      _needs_code(_prow("allow", [], tamper=True)))
check("if the entity key cannot be read, a signed allow's removal asks for the code",
      _needs_code(_prow("allow", _MCP), key=_no_key) == (True, True))

# The decline/approve race: the decline's write only matches a row that is still a draft.
print("   the decline/approve race")
out, revoked, codes = _revoke({"row": None, "rows": [_prow("quarantine", [], approved=False),
                                                     _prow("quarantine", [])],
                               "updates": ["UPDATE 0"]})
check("a quarantine approved between a decline's read and write: nothing is removed without the code",
      isinstance(out, nac_commit.CommitError) and out.status == 403 and not revoked, repr(out))
out, revoked, codes = _revoke({"row": None, "rows": [_prow("quarantine", [], approved=False),
                                                     _prow("quarantine", [])],
                               "updates": ["UPDATE 0"]}, stepup_code="123456")
check("...and with the code it is removed", isinstance(out, dict) and revoked and codes == ["123456"],
      repr(out))
pool = _Pool(_prow("quarantine", [], approved=False))
with _Patch((nac_stepup, "verify_stepup", _verify_code)):
    asyncio.run(nac_commit.revoke_policy(pool, 3, 7))
check("a policy decline's UPDATE only matches a row that is still a draft",
      any("approved_at IS NULL" in q for q, _a in pool.conn.executed), pool.conn.executed)


def _revoke_egress(**kw_pool):
    code = kw_pool.pop("stepup_code", None)
    pool = _Pool(**kw_pool)
    with _Patch((nac_stepup, "verify_stepup", _verify_code)):
        _codes.clear()
        pool, out = _run(lambda: nac_commit.revoke_egress(pool, 3, 4, stepup_code=code), pool)
    return out, any("revoked_at=now()" in q for q, _a in pool.conn.applied), pool


_EDRAFT = {"action": "deny", "approved_at": None}
_EDENY = {"action": "deny", "approved_at": _APPROVED}
out, revoked, _p = _revoke_egress(rows=[_EDRAFT, _EDENY], updates=["UPDATE 0"])
check("an egress deny approved between a decline's read and write: nothing is removed without the code",
      isinstance(out, nac_commit.CommitError) and out.status == 403 and not revoked, repr(out))
out, revoked, _p = _revoke_egress(rows=[_EDRAFT, _EDENY], updates=["UPDATE 0"], stepup_code="123456")
check("...and with the code it is removed", isinstance(out, dict) and revoked, repr(out))
out, revoked, _p = _revoke_egress(row=_EDRAFT)
check("an egress draft decline is free, and its UPDATE only matches a row that is still a draft",
      isinstance(out, dict) and revoked
      and any("approved_at IS NULL" in q for q, _a in _p.conn.executed), _p.conn.executed)

# A re-draft may never rewrite the profile an approved rule points to (legacy unsigned
# rules are read from it, so a free re-draft could turn an approved quarantine into an allow).
pool = _Pool(approved_under_name=41)
e = raises(lambda: asyncio.run(nac_propose.propose_policy(
    pool, name="r", enforcement="allow", permits=[{"lane": "any"}])))
check("a draft under the name of an APPROVED rule is refused", isinstance(e, ValueError), repr(e))
check("...and no profile is written", not any("nac_profiles" in q for q, _a in pool.conn.inserted),
      pool.conn.inserted)
pool = _Pool(approved_under_name=None)
res = asyncio.run(nac_propose.propose_policy(pool, name="r", enforcement="allow", permits=[{"lane": "any"}]))
check("a draft under a name no approved rule uses is still drafted", res.get("status") == "draft")
# End to end on the engine: what the refused re-draft would have done to a legacy unsigned rule.
_legacy = NACController(verify_pubkey=None)
_legacy._ingest([{"id": 5, "name": "r", "enforcement": "allow", "is_default": False,
                  "permits": [{"direction": "inbound", "lane": "any"}]}],
                [{"id": 7, "name": "r", "priority": 1, "match": {}, "profile_id": 5,
                  "signed_terms": None, "signature": None}])
check("(why: a legacy unsigned approved rule is read from its profile, so rewriting that profile "
      "would change what the engine enforces)",
      _legacy.permits_inbound(_legacy.decide({"posture": "healthy"})[0]) is True)

revoke_seen = {}


async def _revoke_capture(pool, eid, rid, **kw):
    revoke_seen.update(kw)
    return {"revoked": True}


with _Patch((nac_commit, "revoke_policy", _revoke_capture), (brain_service, "get_pool", lambda: object())):
    asyncio.run(tools._exec_revoke_nac_rule({"kind": "policy", "id": 7, "stepup_code": "123456"}))
check("revoke_nac_rule passes the code through for a policy", revoke_seen.get("stepup_code") == "123456",
      revoke_seen)
rv = _route_kwargs("nac_commit", "revoke_policy")
check("the Network app's policy revoke route passes the body's code through",
      len(rv) == 1 and rv[0].get("stepup_code") == "<expr>", rv)

# The Network app: an approved quarantine's Revoke button is marked widening, and the
# dispatcher hands that to revokePolicy, which opens the same code prompt as egress.
_js = open(os.path.join(ROOT, "system-apps/network/app.js"), encoding="utf-8").read()
_s, _e = _js.find("function policyEnforcement(r)"), _js.find("// Second-factor chip")
p = subprocess.run(["node", "-e", "const state = {nac: {profiles: []}};\n" + _js[_s:_e] + "\n"
                    "console.log(JSON.stringify(["
                    "policyRevokeWidens({status: 'approved', enforcement: 'quarantine'}),"
                    "policyRevokeWidens({status: 'approved', enforcement: 'allow', permits: [{direction: 'inbound', lane: 'mcp'}]}),"
                    "policyRevokeWidens({status: 'draft', enforcement: 'quarantine'}),"
                    "policyRevokeWidens({status: 'approved', enforcement: 'allow', permits: []}),"
                    "policyRevokeWidens({status: 'approved', enforcement: 'allow', permits: ['mcp']}),"
                    "policyRevokeWidens({status: 'draft', enforcement: 'allow', permits: []})]));"],
                   capture_output=True, text=True, timeout=30)
widen = json.loads(p.stdout) if p.returncode == 0 else p.stderr.strip()[-200:]
check("card: revoking an approved quarantine is widening; an approved allow that admits, or a "
      "draft, is not", isinstance(widen, list) and widen[:3] == [True, False, False], widen)
check("card: revoking an approved allow that admits nothing (no permits, or string permits) is "
      "widening; declining such a draft is not",
      isinstance(widen, list) and widen[3:] == [True, True, False], widen)
check("card: the Ingress Revoke button carries data-widen from policyRevokeWidens",
      'data-nac-revoke="policy:${attr(r.id)}" data-widen="${policyRevokeWidens(r) ? \'1\' : \'0\'}"' in _js)
check("card: the dispatcher passes that to revokePolicy",
      "else if (action === 'revoke') revokePolicy(id, widening);" in _js)
_rp = _js[_js.find("function revokePolicy("):_js.find("function approveGrant(")]
check("card: revokePolicy asks through withStepup and sends stepup_code, like revokeEgress",
      "withStepup(" in _rp and "stepup_code: codeOrNull" in _rp and "}, widening);" in _rp, _rp[:200])

# The skill shows the "any" lane in the same object shape the tool takes.
_skill = open(os.path.join(ROOT, "default-skills/network.md"), encoding="utf-8").read()
check('skill: the "any" lane is shown as [{"lane": "any"}], not a bare string',
      '`[{"lane": "any"}]`' in _skill and '`"any"` is every lane' not in _skill)


# ── 9–13. revoking and replacing an approved rule ────────────────────────────
print("9. the revoke check reads the rule the way the engine loads it")
_rpool = _Pool(_prow("allow", _MCP))
with _Patch((nac_stepup, "verify_stepup", _verify_code), (nac_stepup, "entity_verify_pubkey", _entity_pub)):
    _run(lambda: nac_commit.revoke_policy(_rpool, 3, 7), _rpool)
_read = _rpool.conn.reads[0][0] if _rpool.conn.reads else ""
check("revoke_policy's actual read joins only an UNREVOKED profile, as the engine's load does",
      "ON pr.id = p.profile_id AND pr.revoked_at IS NULL" in " ".join(_read.split()), _read)
_dangling = dict(_prow("allow", _MCP, signed=False), prof_id=None, prof_name=None,
                 prof_enforcement=None, prof_permits=None)
check("a legacy unsigned allow whose profile is revoked reads as dangling (the engine fails the "
      "mesh closed on it), so removing it needs the code", _needs_code(_dangling) == (True, True),
      _needs_code(_dangling))

print("10. the Network app asks for the code exactly when the server will")


class _ListPool2:
    """/nac/policy's two reads: one unrevoked profile, and four rules."""
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, q, *a):
        if "FROM nac_profiles" in q:
            return [{"id": 5, "name": "r", "enforcement": "allow", "is_default": False,
                     "permits": json.dumps(_MCP)}]
        return self.rows


def _listed(row, key=_entity_pub):
    base = {"proposed_by": "host", "approved_by": "user", "stepup_at": None}
    r = {**base, **{k: row[k] for k in ("id", "name", "priority", "match", "profile_id",
                                        "approved_at", "signed_terms", "signature")}}
    with _Patch((nac_stepup, "entity_verify_pubkey", key)):
        out = asyncio.run(nac_commit.policy_list(_ListPool2([r]), 3))["policy"][0]
    return out


_ok = _listed(_prow("allow", _MCP))
check("policy_list: an intact allow that admits → revoke_needs_code False",
      _ok.get("revoke_needs_code") is False, _ok)
check("policy_list: a quarantine → revoke_needs_code True",
      _listed(_prow("quarantine", [])).get("revoke_needs_code") is True)
_t = _listed(_prow("allow", [], tamper=True))
check("policy_list: a row whose signature no longer verifies (terms say 'admits every lane') → "
      "revoke_needs_code True", _t.get("revoke_needs_code") is True and _t.get("permits"), _t)
check("policy_list: when the entity key cannot be read, a signed allow → revoke_needs_code True",
      _listed(_prow("allow", _MCP), key=_no_key).get("revoke_needs_code") is True)
check("policy_list: a draft → revoke_needs_code False (declining is free)",
      _listed(_prow("quarantine", [], approved=False)).get("revoke_needs_code") is False)
check("policy_list sends neither the signature nor the raw signed terms",
      "signature" not in _ok and "signed_terms" not in _ok, sorted(_ok))
# ...and revoke_policy agrees with every one of those rows.
_agree = []
for row in (_prow("allow", _MCP), _prow("quarantine", []), _prow("allow", [], tamper=True),
            _prow("allow", [])):
    flag = _listed(row).get("revoke_needs_code")
    refused = _revoke(row)[0]
    _agree.append(flag == (isinstance(refused, nac_commit.CommitError) and refused.status == 403))
check("revoke_policy refuses without a code exactly where policy_list said revoke_needs_code",
      _agree == [True] * 4, _agree)

p = subprocess.run(["node", "-e", "const state = {nac: {profiles: []}};\n" + _js[_s:_e] + "\n"
                    "console.log(JSON.stringify(["
                    "policyRevokeWidens({status: 'approved', enforcement: 'allow', permits: [{lane: 'mcp'}], revoke_needs_code: true}),"
                    "policyRevokeWidens({status: 'approved', enforcement: 'quarantine', revoke_needs_code: false}),"
                    "policyRevokeWidens({status: 'draft', enforcement: 'quarantine', revoke_needs_code: true}),"
                    "stepupNowRequired({status: 403, message: 'removing this rule ... requires a valid step-up code'}, null),"
                    "stepupNowRequired({status: 403, message: 'removing this rule ... requires a valid step-up code'}, '123456'),"
                    "stepupNowRequired({status: 403, message: \"This Host hasn't granted the Network app that permission.\"}, null)]));"],
                   capture_output=True, text=True, timeout=30)
_c = json.loads(p.stdout) if p.returncode == 0 else p.stderr.strip()[-200:]
check("card: the server's revoke_needs_code decides (an intact-looking allow it flags asks for the "
      "code; a row it clears does not); a draft never asks",
      isinstance(_c, list) and _c[:3] == [True, False, False], _c)
check("card: a free removal refused for want of a code offers the code prompt and a retry; a "
      "coded attempt or a permission refusal does not", isinstance(_c, list) and _c[3:] == [True, False, False], _c)
check("card: revokePolicy and revokeEgress both retry through the prompt on that refusal",
      "if (stepupNowRequired(e, codeOrNull))" in _js[_js.find("function revokePolicy("):_js.find("function approveGrant(")]
      and "if (stepupNowRequired(e, codeOrNull))" in _js[_js.find("function revokeEgress("):_js.find("function approvePolicy(")])

out, revoked, codes = _revoke({"row": None, "rows": [_prow("quarantine", [], approved=False),
                                                     _prow("quarantine", [])],
                               "updates": ["UPDATE 0"]})
check("a policy decline that lost the race says the rule was approved meanwhile and now needs the code",
      isinstance(out, nac_commit.CommitError) and "approved while it was being declined" in out.detail, repr(out))
out, revoked, _p = _revoke_egress(rows=[_EDRAFT, _EDENY], updates=["UPDATE 0"])
check("an egress decline that lost the race says the same",
      isinstance(out, nac_commit.CommitError) and "approved while it was being declined" in out.detail, repr(out))

print("11. the approve half of the race: nothing is left both approved and revoked")
pool, out = _run(lambda: nac_commit.approve_policy(_rp, 3, 7, stepup_code="123456", via="ui"),
                 _rp := _Pool(_policy_row("quarantine", []), updates=["UPDATE 0"]))
check("approving a policy that was declined between the read and the write is refused 409",
      isinstance(out, nac_commit.CommitError) and out.status == 409, repr(out))
check("...and its UPDATE only matches an unrevoked draft",
      any("approved_at IS NULL AND revoked_at IS NULL" in q for q, _a in pool.conn.executed), pool.conn.executed)
_erow = {"id": 4, "pattern": "evil.example", "action": "deny", "priority": 100, "subject": "*",
         "exit": "default", "approved_at": None}
pool, out = _run(lambda: nac_commit.approve_egress(_ep, 3, 4, via="ui"),
                 _ep := _Pool(_erow, updates=["UPDATE 0"]))
check("approving an egress rule that was declined between the read and the write is refused 409",
      isinstance(out, nac_commit.CommitError) and out.status == 409, repr(out))
check("...and its UPDATE only matches an unrevoked draft",
      any("approved_at IS NULL AND revoked_at IS NULL" in q for q, _a in pool.conn.executed), pool.conn.executed)
pool, out = _run(lambda: nac_commit.approve_egress(_ep2, 3, 4, via="ui"), _ep2 := _Pool(_erow))
check("an approval with no race still succeeds", isinstance(out, dict) and out.get("approved"), repr(out))

print("12. the words match the rule")
_routes_src = open(os.path.join(ROOT, "server/routes/network.py"), encoding="utf-8").read()
_route_doc = _routes_src[_routes_src.find("async def nac_policy_revoke"):][:700]
check("the policy revoke route's docstring names every rule that keeps machines out",
      "allow that admits no lane" in _route_doc and "Removing an approved QUARANTINE widens" not in _route_doc,
      _route_doc[:300])
check("revoke_nac_rule's executor docstring names them too",
      "revoke_needs_code" in (tools._exec_revoke_nac_rule.__doc__ or ""), tools._exec_revoke_nac_rule.__doc__)
_rv = PUBLIC_TOOL_SCHEMAS["revoke_nac_rule"]["function"]
check("revoke_nac_rule's description points at revoke_needs_code, not 'quarantine' alone",
      "revoke_needs_code" in _rv["description"] and "ingress quarantine" not in _rv["description"],
      _rv["description"])

print("13. replacing an approved rule: new name, approved, then revoke the old")
e = raises(lambda: asyncio.run(nac_propose.propose_policy(
    _Pool(approved_under_name=41), name="r", enforcement="allow", permits=[{"lane": "any"}])))
check("the refusal tells the Host to draft under a new name, get it approved, then revoke the old",
      isinstance(e, ValueError) and "new name" in str(e) and "then revoke the old one" in str(e)
      and "or revoke that rule first" not in str(e), repr(e))
check("the skill says the same",
      "To\n  replace an approved rule, draft the new one under a new name, have the user\n  approve it, then revoke the old one" in _skill
      or "replace an approved rule, draft the new one under a new name" in _skill.replace("\n  ", " "))


# ── 14–18. management grants and DCN stores ──────────────────────────────────
print("14. management grants: neither half of the decline/approve race slips a deny")
_GDRAFT = {"id": 6, "approved_at": None, "signed_terms": None}
_GDENY = {"id": 6, "approved_at": _APPROVED,
          "signed_terms": json.dumps({"server": "box1", "ops": [], "deny": ["contain"]})}
_GALLOW = {"id": 6, "approved_at": _APPROVED,
           "signed_terms": json.dumps({"server": "box1", "ops": ["uptime"], "deny": []})}


def _revoke_grant(stepup_code=None, **kw_pool):
    pool = _Pool(**kw_pool)
    with _Patch((nac_stepup, "verify_stepup", _verify_code)):
        _codes.clear()
        pool, out = _run(lambda: nac_commit.revoke_mgmt_grant(pool, 3, 6, stepup_code=stepup_code), pool)
    return out, any("revoked_at=now()" in q for q, _a in pool.conn.applied), pool


out, revoked, _g = _revoke_grant(rows=[_GDRAFT, _GDENY], updates=["UPDATE 0"])
check("a deny grant approved between a decline's read and write: nothing is removed without the code",
      isinstance(out, nac_commit.CommitError) and out.status == 403 and not revoked, repr(out))
check("...and the refusal says it was approved while being declined",
      isinstance(out, nac_commit.CommitError) and "approved while it was being declined" in out.detail, repr(out))
out, revoked, _g = _revoke_grant(rows=[_GDRAFT, _GDENY], updates=["UPDATE 0"], stepup_code="123456")
check("...and with the code it is removed", isinstance(out, dict) and revoked and _codes == ["123456"], repr(out))
out, revoked, _g = _revoke_grant(rows=[_GDRAFT, _GALLOW], updates=["UPDATE 0"])
check("an allow-only grant approved during the decline is still removed free (a tighten)",
      isinstance(out, dict) and revoked and _codes == [], (repr(out), _codes))
out, revoked, _g = _revoke_grant(row=_GDRAFT)
check("a grant draft decline is free, and its UPDATE only matches a row that is still a draft",
      isinstance(out, dict) and revoked
      and any("approved_at IS NULL" in q for q, _a in _g.conn.executed), _g.conn.executed)
out, revoked, _g = _revoke_grant(row=_GDENY)
check("removing an approved deny grant with no race still needs the code",
      isinstance(out, nac_commit.CommitError) and out.status == 403 and not revoked, repr(out))

_grow = {"id": 6, "server": "box1", "ops": "[]", "deny": '["contain"]', "intent": None, "approved_at": None}
pool, out = _run(lambda: nac_commit.approve_mgmt_grant(_gp, 3, 6, via="ui"),
                 _gp := _Pool(_grow, updates=["UPDATE 0"]))
check("approving a grant that was declined between the read and the write is refused 409",
      isinstance(out, nac_commit.CommitError) and out.status == 409, repr(out))
check("...and its UPDATE only matches an unrevoked draft",
      any("approved_at IS NULL AND revoked_at IS NULL" in q for q, _a in pool.conn.executed), pool.conn.executed)
pool, out = _run(lambda: nac_commit.approve_mgmt_grant(_gp2, 3, 6, via="ui"), _gp2 := _Pool(_grow))
check("a grant approval with no race still succeeds", isinstance(out, dict) and out.get("approved"), repr(out))
check("skill: the decline/approve race is described for all three kinds",
      "free for all three kinds (policy, egress, management\n  grant)" in _skill
      and "refused (409)" in _skill)

print("15. a DCN store is never reported granted when it is not in force")


class _RollbackTx:
    """A transaction that behaves like one: writes inside it are held, committed when the
    block exits cleanly, and DISCARDED when it raises — so a check can tell a rolled-back
    write from a committed one."""
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.buf = []
        return self

    async def __aexit__(self, exc_type, *a):
        buf, self.conn.buf = self.conn.buf, None
        if exc_type is None:
            self.conn.committed.extend(buf)
        return False


class _StoreConn(_Conn):
    """approve_dcn_store's reads: the store row, then each bundled egress rule (`egress` may
    be a list, one answer per read, the last repeating). Writes land in `committed` only
    when their transaction commits, or at once outside one."""
    def __init__(self, store, egress=None, updates=None):
        super().__init__(None, updates=updates)

        def _cols(r):  # the columns every one of these tables has, which the reads select
            return {"revoked_at": None, "approved_by": None, "signed_terms": None, **r} \
                if isinstance(r, dict) else r
        self.store = _cols(store)
        self.egress = [_cols(e) for e in (egress if isinstance(egress, list) else [egress])]
        self.committed, self.buf = [], None

    async def fetchrow(self, q, *a):
        self.reads.append((q, a))
        if "dcn_stores" in q:
            return self.store
        return self.egress.pop(0) if len(self.egress) > 1 else self.egress[0]

    async def execute(self, q, *a):
        status = await super().execute(q, *a)
        if not status.endswith(" 0"):
            (self.buf if self.buf is not None else self.committed).append((q, a))
        return status

    def transaction(self):
        return _RollbackTx(self)


class _StorePool(_Pool):
    def __init__(self, store, egress=None, updates=None):
        self.conn = _StoreConn(store, egress, updates)
        self.acquired = 0


_store = {"id": 8, "class": "cabinet", "mask": '["read"]', "excludes": "[]", "sweep": "hourly",
          "sensitive": False, "granted_to": None, "bucket": "b", "prefix": "", "endpoint": None,
          "cred_name": "ctx:s3", "server": None, "root": None, "approved_at": None,
          "egress_rule_ids": "[4]", "display_name": "B", "note": None}
_bundled = {"id": 4, "pattern": "b.s3.amazonaws.com", "action": "allow", "priority": 100,
            "subject": "*", "exit": "default", "approved_at": None}


def _approve_store(store=_store, egress=_bundled, patches=(), **kw):
    pool = _StorePool(store, egress, **kw)
    with _Patch((nac_stepup, "verify_stepup", _verify_code), *patches):
        _codes.clear()
        return _run(lambda: nac_commit.approve_dcn_store(pool, 3, 8, stepup_code="123456"), pool)


def _granted(pool):
    """A committed approval of the store row."""
    return any("UPDATE dcn_stores SET approved_at" in q for q, _a in pool.conn.committed)


pool, out = _approve_store(updates=["UPDATE 0"])
check("approving a store that was declined between the read and the write is refused 409",
      isinstance(out, nac_commit.CommitError) and out.status == 409, repr(out))
check("...and its UPDATE only matches an unrevoked draft",
      any("dcn_stores" in q and "approved_at IS NULL AND revoked_at IS NULL" in q
          for q, _a in pool.conn.executed), pool.conn.executed)
pool, out = _approve_store(updates=["UPDATE 1", "UPDATE 0"])
check("a store whose bundled egress rule was declined meanwhile is refused 409, and the store's "
      "own approval is ROLLED BACK with it (the composite commits whole or not at all)",
      isinstance(out, nac_commit.CommitError) and out.status == 409 and not _granted(pool)
      and any("UPDATE dcn_stores SET approved_at" in q for q, _a in pool.conn.applied),
      (repr(out), pool.conn.committed))
check("...and the bundled egress UPDATE only matches an unrevoked draft",
      any("nac_egress" in q and "approved_at IS NULL AND revoked_at IS NULL" in q
          for q, _a in pool.conn.executed), pool.conn.executed)
pool, out = _approve_store()
check("a store approval with no race grants the store and its bundled egress, both committed",
      isinstance(out, dict) and out.get("approved") and out.get("bundled_egress") and _granted(pool)
      and any("UPDATE nac_egress SET approved_at" in q for q, _a in pool.conn.committed),
      (repr(out), pool.conn.committed))

print("16. a store is never granted without the egress it was drafted with")
pool, out = _approve_store(egress=None)
check("a store whose bundled egress rule was DECLINED before the approval is refused 409, "
      "with a clear instruction to re-draft",
      isinstance(out, nac_commit.CommitError) and out.status == 409
      and "draft it again" in out.detail, repr(out))
check("...before any code is spent, and nothing is committed", _codes == [] and pool.conn.committed == [],
      (_codes, pool.conn.committed))
pool, out = _approve_store(egress=[_bundled, None])
check("a bundled rule declined between that check and its own approval: 409, store rolled back",
      isinstance(out, nac_commit.CommitError) and out.status == 409 and not _granted(pool),
      (repr(out), pool.conn.committed))
pool, out = _approve_store(egress=dict(_bundled, approved_at=_APPROVED))
check("a bundled rule already approved on its own is fine: the store is granted",
      isinstance(out, dict) and out.get("approved") and _granted(pool), repr(out))

print("17. a machine's refusal clears the stored request, and the clear survives the rollback")
from server.network import nac_approval   # noqa: E402

_mstore = {"id": 8, "class": "machine", "mask": '["read"]', "excludes": "[]", "sweep": "hourly",
           "sensitive": False, "granted_to": None, "server": "box1", "root": "/data",
           "bucket": None, "prefix": None, "endpoint": None, "cred_name": None, "approved_at": None,
           "egress_rule_ids": "[]", "display_name": "Docs", "note": None,
           "box_request": json.dumps({"via": "device", "id": "r1", "request": "r1", "purpose": "p"})}
_seen_existing = []


def _conf(state):
    async def f(pool, eid, *, server, purpose, details, existing=None, **kw):
        _seen_existing.append(existing)
        return {"state": state, "detail": f"the machine said {state}"}
    return f


def _cleared(pool):
    return any("box_request=NULL" in q and "approved_at" not in q.split("WHERE")[0]
               for q, _a in pool.conn.committed)


for state, status in (("denied", 403), ("expired", 403)):
    pool, out = _approve_store(store=_mstore, egress=None,
                               patches=((nac_approval, "grant_confirmation", _conf(state)),))
    check(f"a {state} request refuses the approval ({status})",
          isinstance(out, nac_commit.CommitError) and out.status == status, repr(out))
    check(f"...and the stored request is CLEARED and stays cleared ({state}) — so the next "
          f"approval files a new request instead of re-reading a {state} id forever",
          _cleared(pool), pool.conn.committed)
pool, out = _approve_store(store=_mstore, egress=None,
                           patches=((nac_approval, "grant_confirmation", _conf("offline")),))
check("an offline machine refuses 503 and KEEPS its request (it may still be answered there)",
      isinstance(out, nac_commit.CommitError) and out.status == 503 and not _cleared(pool),
      (repr(out), pool.conn.committed))
check("(why it matters: the stored request is what the next approval re-checks)",
      _seen_existing and isinstance(_seen_existing[0], dict) and _seen_existing[0].get("id") == "r1",
      _seen_existing[:1])

print("18. the words and the grant card match what the server does")
_skill_flat = " ".join(_skill.split())
check("skill: a draft approved during a decline is removed at once when that only tightens, and "
      "stays in force until the code when it widens (never 'nothing is removed')",
      "removed at once when removing it only tightens" in _skill_flat
      and "left in force until the user's code is given when removing it widens" in _skill_flat
      and "nothing is removed and it is decided" not in _skill_flat, _skill_flat[-600:])
_rg = _js[_js.find("function revokeGrant("):_js.find("function nacDispatch(")]
check("card: a grant decline that lost the race reloads and offers the code prompt, like policy and egress",
      "if (stepupNowRequired(e, codeOrNull))" in _rg and "nacReload();" in _rg
      and "revokeGrant(id, true, true)" in _rg, _rg[-500:])


# ── 19–21. a refusal clears only its own request ─────────────────────────────
print("19. a refusal clears only the request it refused, never a newer one")


class _Db:
    """One stored row with real state, shared by every connection — enough SQL is
    interpreted to run the box_request writes these approve paths make. `lock=True` models
    SELECT ... FOR UPDATE: the reader holds the row until its transaction ends, and any
    other FOR UPDATE reader waits."""
    def __init__(self, row, lock=False):
        self.row = dict(row)
        # Columns every one of these tables has, which the approve paths' reads select.
        for col in ("revoked_at", "approved_by", "signed_terms"):
            self.row.setdefault(col, None)
        self.log = []          # every write that changed the row, in order
        self.lock = asyncio.Lock() if lock else None

    def _blocked(self, flat):
        """A guarded write's WHERE, read from the query text, judged against the committed row."""
        return (("approved_at IS NULL" in flat and self.row.get("approved_at") is not None)
                or ("revoked_at IS NULL" in flat and self.row.get("revoked_at") is not None))

    def apply(self, q, a, dry=False):
        """Run (or, with dry=True, only evaluate) one write. A guarded write's WHERE is judged
        against the COMMITTED row, as Postgres re-checks it at UPDATE time; an unguarded one
        overwrites, as it would there."""
        flat = " ".join(q.split())
        done = self.row.get("approved_at") is not None or self.row.get("revoked_at") is not None
        if " SET revoked_at=now()" in flat:
            if self._blocked(flat):
                return "UPDATE 0"
            if not dry:
                self.row["revoked_at"] = "now"
                self.log.append(("revoke", flat))
            return "UPDATE 1"
        if " SET approved_at=now()" in flat:
            if self._blocked(flat):
                return "UPDATE 0"
            if not dry:
                self.row.update(approved_at="now", approved_by=a[1], status="granted",
                                box_request=None, signed_terms=a[3] if len(a) > 3 else None)
                self.log.append(("approve", flat))
            return "UPDATE 1"
        if "SET box_request=$2::jsonb" in flat:
            if "approved_at IS NULL" in flat and done:
                return "UPDATE 0"
            if not dry:
                self.row["box_request"] = a[1]
                self.log.append(("store_request", flat))
            return "UPDATE 1"
        if "SET box_request=NULL" in flat:
            if self.row.get("approved_at") is not None:
                return "UPDATE 0"
            if "box_request->>'request' = $2" in flat:
                held = self.row.get("box_request")
                held = json.loads(held) if isinstance(held, str) else held
                if not (isinstance(held, dict) and held.get("request") == a[1]):
                    return "UPDATE 0"
            if not dry:
                self.row["box_request"] = None
                self.log.append(("clear", flat))
            return "UPDATE 1"
        return "UPDATE 1"


class _DbConn:
    def __init__(self, db):
        self.db, self.buf, self.holds = db, None, False

    async def fetchrow(self, q, *a):
        if "FOR UPDATE" in q and self.db.lock is not None and not self.holds:
            await self.db.lock.acquire()
            self.holds = True
        return dict(self.db.row)

    async def execute(self, q, *a):
        if self.buf is not None:
            status = self.db.apply(q, a, dry=True)
            if not status.endswith(" 0"):
                self.buf.append((q, a))
            return status
        return self.db.apply(q, a)

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.buf = []

            async def __aexit__(self, exc_type, *e):
                buf, conn.buf = conn.buf, None
                if exc_type is None:
                    for q, a in buf:
                        conn.db.apply(q, a)
                if conn.holds:
                    conn.holds = False
                    conn.db.lock.release()
                return False
        return _Tx()


class _DbPool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        db = self.db

        class _Acq:
            async def __aenter__(self):
                return _DbConn(db)

            async def __aexit__(self, *e):
                return False
        return _Acq()

    async def execute(self, q, *a):
        return self.db.apply(q, a)

    async def fetchrow(self, q, *a):
        return dict(self.db.row)

    async def fetch(self, q, *a):
        """complete_box_requests' selection, evaluating whichever server predicate the code
        sends: the request's own `server` key alone, or that key falling back to the
        store's server (COALESCE). Any other shape selects nothing, so a changed query
        fails here rather than passing by accident."""
        flat = " ".join(q.split())
        row = self.db.row
        if "FROM dcn_stores" not in flat or "dcn_move_plans" in flat:
            return []
        if row.get("approved_at") is not None or row.get("revoked_at") is not None:
            return []
        held = row.get("box_request")
        held = json.loads(held) if isinstance(held, str) else held
        if not isinstance(held, dict):
            return []
        if "COALESCE(box_request->>'server', server) = $2" in flat:
            key = held.get("server") or row.get("server")
        elif "box_request->>'server' = $2" in flat:
            key = held.get("server")
        else:
            return []
        return [{"id": row["id"]}] if key == a[1] else []


_R1 = json.dumps({"request": "R1", "server": "box1", "purpose": "p"})


async def _interleave():
    """The heartbeat (complete_box_requests → approve_dcn_store) is slow on R1 while the
    person clicks Approve twice: the first click's refusal clears R1, the second files R2.
    Then the heartbeat's R1 refusal lands. R2 must still be stored."""
    db = _Db(dict(_mstore, box_request=_R1))
    pool = _DbPool(db)
    heartbeat_may_answer = asyncio.Event()
    calls = []

    async def conf(pool_, eid, *, server, purpose, details, existing=None, **kw):
        calls.append((existing or {}).get("request"))
        if existing and existing.get("request") == "R1":
            if len(calls) == 1:                       # the heartbeat: slow
                await heartbeat_may_answer.wait()
            return {"state": "denied", "detail": "you refused this on box1"}
        return {"state": "pending", "request": {"request": "R2", "server": "box1", "purpose": purpose}}

    async def attempt():
        try:
            return await nac_commit.approve_dcn_store(pool, 3, 8, approved_by="user@machine")
        except nac_commit.CommitError as e:
            return e

    with _Patch((nac_approval, "grant_confirmation", conf)):
        heartbeat = asyncio.ensure_future(attempt())
        await asyncio.sleep(0)                        # the heartbeat reads R1 and waits
        first_click = await attempt()                 # R1 refused → cleared
        second_click = await attempt()                # files R2
        heartbeat_may_answer.set()
        late = await heartbeat
    held = db.row.get("box_request")
    held = json.loads(held) if isinstance(held, str) else held
    return first_click, second_click, late, held, calls


first_click, second_click, late, held, calls = asyncio.run(_interleave())
check("the interleaving ran as staged (heartbeat on R1, click on R1, click filing R2)",
      calls == ["R1", "R1", None], calls)
check("...the first click was refused, the second filed R2",
      isinstance(first_click, nac_commit.CommitError) and isinstance(second_click, dict)
      and second_click.get("pending_on_box"), (first_click, second_click))
check("the heartbeat's late refusal of R1 does NOT clear R2: the newer request stays stored, so its "
      "approval is picked up", isinstance(late, nac_commit.CommitError)
      and isinstance(held, dict) and held.get("request") == "R2", held)

print("20. the same rolled-back clear is fixed for move plans and reach grants")
from server.network import service_reach   # noqa: E402

_plan = {"id": 5, "store_id": 8, "archive": "Archive", "title": "Cleanup",
         "moves": json.dumps([{"from": "a.txt", "to": "Archive/a.txt"}]), "approved_at": None,
         "store_status": "granted", "store_revoked": None, "server": "box1", "root": "/data",
         "box_request": json.dumps({"request": "P1", "server": "box1", "purpose": "p"})}


async def _plan_denied(pool, eid, *, server, purpose, details, existing=None, **kw):
    return {"state": "denied", "detail": "you refused this on box1"}


def _cleared_for(pool, table, rid):
    return any(f"UPDATE {table} SET box_request=NULL" in " ".join(q.split())
               and "box_request->>'request' = $2" in q and a[1] == rid
               for q, a in pool.conn.committed)


pool = _StorePool(_plan, _plan)   # the approve read (joins dcn_stores) and the re-read both find it
with _Patch((nac_stepup, "box_confirmation", _plan_denied)):
    pool, out = _run(lambda: nac_commit.approve_dcn_move_plan(pool, 3, 5), pool)
check("a denied move plan refuses 403", isinstance(out, nac_commit.CommitError) and out.status == 403, repr(out))
check("...and its stored request is cleared, committed despite the rollback, matched on the refused id",
      _cleared_for(pool, "dcn_move_plans", "P1"), pool.conn.committed)

_reach = {"id": 9, "server": "box1", "slot": "web", "subject": "host.example", "note": None,
          "mirrors": None, "approved_at": None,
          "box_request": json.dumps({"via": "device", "id": "G1", "request": "G1", "purpose": "p"})}
pool = _StorePool(_reach, _reach)
try:
    with _Patch((nac_approval, "grant_confirmation", _plan_denied)):
        asyncio.run(service_reach.approve_reach(pool, 3, 9))
    out = None
except (service_reach.ReachError, TypeError) as e:
    out = e
check("a denied reach grant refuses 403", isinstance(out, service_reach.ReachError) and out.status == 403,
      repr(out))
check("...and its stored request is cleared, committed despite the rollback, matched on the refused id",
      _cleared_for(pool, "service_reach_grants", "G1"), pool.conn.committed)

print("21. the store's re-draft instruction names the Context Manager's button")
pool, out = _approve_store(egress=None)
check("the 409 says 'Discard this store and draft it again' (the draft's button is 'Discard')",
      isinstance(out, nac_commit.CommitError) and "Discard this store and draft it again" in out.detail,
      repr(out))


# ── 22. device approvals complete on the heartbeat ───────────────────────────
print("22. a request filed through an approval device is completed by the heartbeat")
from server.network import nac_dcn   # noqa: E402

_DEVICE = {"hostname": "phone", "token_id": 7}
_device_log = []


async def _gate(pool, server, token_id=None):
    return {"write_gate": True, "device": True, "device_keys": ["k"], "stepup": False,
            "second_factor": True, "seen": True}


async def _find(pool, eid, keys):
    return _DEVICE


async def _file(pool, eid, device, details, server):
    _device_log.append(("file", server))
    return {"state": "pending", "id": "D1"}


async def _status_approved(pool, eid, device, rid):
    _device_log.append(("status", rid))
    return {"state": "approved", "approval": f"APPROVAL-{rid}"}


_DEVICE_PATCHES = ((nac_approval, "box_gate", _gate), (nac_approval, "find_device", _find),
                   (nac_approval, "_device_file", _file),
                   (nac_approval, "_device_status", _status_approved),
                   (nac_dcn, "sync_dcn_grant", _noop))
_store_purpose = nac_stepup.purpose_for_store_terms(
    {"server": "box1", "root": "/data", "mask": ["read"], "excludes": [], "sweep": "hourly",
     "sensitive": False, "ttl_days": 365})


async def _device_flow(stored_request=None):
    """Click Approve (files on the device, unless a request is already stored), then the
    governed machine's heartbeat runs. Returns (click result, stored request, heartbeat
    result, final row)."""
    db = _Db(dict(_mstore, box_request=stored_request))
    pool = _DbPool(db)
    click = None
    with _Patch(*_DEVICE_PATCHES):
        if stored_request is None:
            try:
                click = await nac_commit.approve_dcn_store(pool, 3, 8)
            except nac_commit.CommitError as e:
                click = e
        filed = db.row.get("box_request")
        filed = json.loads(filed) if isinstance(filed, str) else filed
        beat = await nac_commit.complete_box_requests(pool, 3, "box1")
    return click, filed, beat, db.row


_device_log.clear()
click, filed, beat, final = asyncio.run(_device_flow())
check("clicking Approve on a device-governed store files the request on the device",
      isinstance(click, dict) and click.get("pending_on_box") and ("file", "box1") in _device_log,
      (click, _device_log))
check("the stored device request carries the same keys as a console one, `server` (the governed "
      "machine) included", isinstance(filed, dict) and filed.get("via") == "device"
      and filed.get("server") == "box1" and filed.get("request") == "D1" and filed.get("at"), filed)
check("the heartbeat from that machine picks the device request up and completes the approval",
      beat == {"stores": [8], "plans": []} and final.get("approved_at") is not None
      and ("status", "D1") in _device_log, (beat, final.get("approved_at"), _device_log))
check("...signing the device's approval into the terms",
      '"approval": "APPROVAL-D1"' in (final.get("signed_terms") or ""), final.get("signed_terms"))

_legacy_req = json.dumps({"via": "device", "device": "phone", "id": "D0", "request": "D0",
                          "purpose": _store_purpose})
_device_log.clear()
click, filed, beat, final = asyncio.run(_device_flow(stored_request=_legacy_req))
check("a LEGACY device request stored without `server` is still found by the heartbeat (the store's "
      "server is used) and completes", beat == {"stores": [8], "plans": []}
      and final.get("approved_at") is not None and ("status", "D0") in _device_log,
      (beat, final.get("approved_at"), _device_log))
_other = json.dumps({"via": "device", "id": "D2", "request": "D2", "purpose": _store_purpose})
db = _Db(dict(_mstore, box_request=_other))
with _Patch(*_DEVICE_PATCHES):
    beat_elsewhere = asyncio.run(nac_commit.complete_box_requests(_DbPool(db), 3, "box2"))
check("...and another machine's heartbeat does not pick it up", beat_elsewhere == {"stores": [], "plans": []}
      and db.row.get("approved_at") is None, beat_elsewhere)
_plan_q = []


class _PlanQueryPool(_DbPool):
    async def fetch(self, q, *a):
        if "dcn_move_plans" in q:
            _plan_q.append(" ".join(q.split()))
            return []
        return await super().fetch(q, *a)


asyncio.run(nac_commit.complete_box_requests(_PlanQueryPool(_Db(_mstore)), 3, "box1"))
check("a pending move plan is matched the same way, falling back to its store's server",
      _plan_q and "COALESCE(p.box_request->>'server', s.server) = $2" in _plan_q[0], _plan_q)


# ── 23. two approvals of one request at once ────────────────────────────────
print("23. two approvals of one request at once: no false success, and the loser reports the "
      "approval when the winner has committed by the time it re-reads")


def _racing_confirm(loser_answer, approval="APPROVAL-X"):
    """A confirmation stub for two concurrent approvals of one stored request: the FIRST
    caller (the loser) is slow and, released after the other has won, gets `loser_answer` —
    what the machine gives once its one-time approval has been handed out (the device lane
    reads "delivered" as lapsed; the console lane files a fresh ask). The second caller
    gets the approval."""
    release = asyncio.Event()
    calls = []

    async def conf(*args, existing=None, **kw):
        calls.append((existing or {}).get("request"))
        if len(calls) == 1:
            await release.wait()
            return dict(loser_answer)
        return {"state": "approved", "approval": approval,
                "request": existing or {"request": "X"}}
    return conf, release, calls


async def _race(pool_factory, approve, patch_target, loser_answer):
    db, pool = pool_factory()
    conf, release, calls = _racing_confirm(loser_answer)
    with _Patch(patch_target(conf), (nac_stepup, "verify_stepup", _verify_code)):
        async def attempt():
            try:
                return await approve(pool)
            except Exception as e:     # a refusal is the bug this checks for
                return e
        loser = asyncio.ensure_future(attempt())      # the heartbeat: reads, then waits
        await asyncio.sleep(0)
        winner = await attempt()                      # the click: approved
        release.set()
        late = await loser
    return winner, late, db, calls


_EXPIRED = {"state": "expired", "detail": "the request lapsed on the device"}
_PENDING = {"state": "pending", "request": {"request": "FRESH", "server": "box1", "purpose": "p"}}


def _db_pool(row):
    def make():
        db = _Db(row)
        return db, _DbPool(db)
    return make


def _check_race(label, winner, late, db, calls):
    check(f"{label}: the race ran as staged (both read the same stored request)",
          len(calls) == 2 and calls[0] == calls[1], calls)
    check(f"{label}: the click approves it",
          isinstance(winner, dict) and winner.get("approved") is True, repr(winner))
    check(f"{label}: the late one returns the APPROVED result, not a refusal",
          isinstance(late, dict) and late.get("approved") is True and late.get("approved_concurrently"),
          repr(late))
    check(f"{label}: nothing was cleared or re-stored after the approval, and it holds",
          db.row.get("approved_at") is not None and db.row.get("box_request") is None
          and [k for k, _q in db.log] == ["approve"], db.log)


_race_store = dict(_mstore, box_request=json.dumps(
    {"via": "device", "id": "D5", "request": "D5", "server": "box1", "purpose": _store_purpose}))
w, l, d, c = asyncio.run(_race(_db_pool(_race_store),
                               lambda p: nac_commit.approve_dcn_store(p, 3, 8, approved_by="user@machine"),
                               lambda f: (nac_approval, "grant_confirmation", f), _EXPIRED))
_check_race("store, device lane (loser reads 'delivered' as lapsed)", w, l, d, c)


# The console lane, through the REAL box_confirmation against a simulated box that hands its
# approval out once and answers the next status read "delivered" (bastion stepup.go
# handleStepupStatus). The loser must report the approval and file NO second stepup_request
# — an orphan ask would be refused for 24h if denied, or hand a stale approval to the next
# identical draft if approved.
def _console_box():
    state = {"handed": False, "log": [], "status_calls": 0}
    release = asyncio.Event()

    async def dispatch(pool, eid, server, op, args, token_id=None):
        state["log"].append(op)
        if op == "stepup_status":
            state["status_calls"] += 1
            if state["status_calls"] == 1:          # the loser: slow
                await release.wait()
            if not state["handed"]:
                state["handed"] = True
                return {"exit": 0, "stdout": json.dumps({"state": "approved", "approval": "BOX-APPROVAL"})}
            return {"exit": 0, "stdout": json.dumps({"state": "delivered"})}
        if op == "stepup_request":
            return {"exit": 0, "stdout": json.dumps({"state": "pending", "request": "ORPHAN"})}
        return {"exit": 0, "stdout": "{}"}
    return state, release, dispatch


async def _enrolled(pool, server):
    return True


async def _console_gate(pool, server, token_id=None):
    return {"write_gate": True, "device": False, "device_keys": [], "stepup": True,
            "second_factor": True, "seen": True}


async def _console_race(row, approve):
    db = _Db(row)
    pool = _DbPool(db)
    box, release, dispatch = _console_box()
    with _Patch((nac_approval, "box_gate", _console_gate), (nac_stepup, "box_stepup_enrolled", _enrolled),
                (nac_dcn, "_dispatch_box", dispatch)):
        async def attempt():
            try:
                return await approve(pool)
            except Exception as e:
                return e
        loser = asyncio.ensure_future(attempt())
        await asyncio.sleep(0)
        winner = await attempt()
        release.set()
        late = await loser
    return winner, late, db, box["log"]


_plan_purpose = nac_stepup.purpose_for_plan_terms(
    {"store_id": 8, "plan_id": 5, "root": "/data", "archive": "Archive",
     "moves": [{"from": "a.txt", "to": "Archive/a.txt"}]})
for label, row, approve in (
        ("store, console lane", dict(_mstore, box_request=json.dumps(
            {"request": "C5", "server": "box1", "purpose": _store_purpose})),
         lambda p: nac_commit.approve_dcn_store(p, 3, 8, approved_by="user@machine")),
        ("move plan, console lane", dict(_plan, box_request=json.dumps(
            {"request": "C6", "server": "box1", "purpose": _plan_purpose})),
         lambda p: nac_commit.approve_dcn_move_plan(p, 3, 5, approved_by="user@machine"))):
    w, l, d, log = asyncio.run(_console_race(row, approve))
    check(f"{label}: the click approves it with the box's one-time approval",
          isinstance(w, dict) and w.get("approved") is True, repr(w))
    check(f"{label}: the late one reads 'delivered' and returns the APPROVED result",
          isinstance(l, dict) and l.get("approved") is True and l.get("approved_concurrently"), repr(l))
    check(f"{label}: NO second stepup_request is sent to the machine (no orphan ask)",
          log == ["stepup_status", "stepup_status"], log)
    check(f"{label}: nothing was cleared or re-stored after the approval",
          d.row.get("approved_at") is not None and d.row.get("box_request") is None
          and [k for k, _q in d.log] == ["approve"], d.log)

# Defensive: a machine answering "pending" for a row approved meanwhile (not what the box does
# today — "delivered" is reported now — but the pending write is guarded and must hold).
w, l, d, c = asyncio.run(_race(_db_pool(_race_store),
                               lambda p: nac_commit.approve_dcn_store(p, 3, 8, approved_by="user@machine"),
                               lambda f: (nac_approval, "grant_confirmation", f), _PENDING))
_check_race("store, defensive: a pending answer on a store approved meanwhile", w, l, d, c)

_race_plan = dict(_plan, box_request=json.dumps({"request": "P5", "server": "box1", "purpose": "p"}))
w, l, d, c = asyncio.run(_race(_db_pool(_race_plan),
                               lambda p: nac_commit.approve_dcn_move_plan(p, 3, 5, approved_by="user@machine"),
                               lambda f: (nac_stepup, "box_confirmation", f), _PENDING))
_check_race("move plan, defensive: a pending answer on a plan approved meanwhile", w, l, d, c)
w, l, d, c = asyncio.run(_race(_db_pool(_race_plan),
                               lambda p: nac_commit.approve_dcn_move_plan(p, 3, 5, approved_by="user@machine"),
                               lambda f: (nac_stepup, "box_confirmation", f), _EXPIRED))
_check_race("move plan, refused lane", w, l, d, c)

_race_reach = dict(_reach, box_request=json.dumps(
    {"via": "device", "id": "G5", "request": "G5", "server": "box1", "purpose": "p"}))
# Defensive only: this interleaving (the loser holding the row while the other approves) is
# one FOR UPDATE forbids in Postgres. The fake here takes no lock; the case below does.
w, l, d, c = asyncio.run(_race(_db_pool(_race_reach),
                               lambda p: service_reach.approve_reach(p, 3, 9, approved_by="user@machine"),
                               lambda f: (nac_approval, "grant_confirmation", f), _EXPIRED))
_check_race("reach grant, defensive (an ordering FOR UPDATE forbids)", w, l, d, c)


async def _reach_locked():
    """The REAL reach outcome: SELECT ... FOR UPDATE holds the row while the first approval
    asks the machine; the second waits on the lock, then finds the grant approved."""
    db = _Db(_race_reach, lock=True)
    pool = _DbPool(db)
    release = asyncio.Event()
    asked = []

    async def conf(*a, existing=None, **kw):
        asked.append((existing or {}).get("request"))
        await release.wait()
        return {"state": "approved", "approval": "DEVICE-APPROVAL", "request": existing}

    async def attempt():
        try:
            return await service_reach.approve_reach(pool, 3, 9, approved_by="user@machine")
        except service_reach.ReachError as e:
            return e
    with _Patch((nac_approval, "grant_confirmation", conf)):
        first = asyncio.ensure_future(attempt())
        await asyncio.sleep(0)
        second = asyncio.ensure_future(attempt())
        await asyncio.sleep(0)
        release.set()
        return await first, await second, asked, db


first, second, asked, d = asyncio.run(_reach_locked())
check("reach grant, with the row lock: the first approval is granted",
      isinstance(first, dict) and first.get("approved") is True, repr(first))
check("...the second waited on the lock and gets 409 'already approved' (it never asked the machine)",
      isinstance(second, service_reach.ReachError) and second.status == 409
      and "already approved" in second.detail and asked == ["G5"], (repr(second), asked))


# ── 24. management ops and the session lane keep re-filing on "delivered" ─────
print("24. only the grant approve paths report 'delivered'; management ops and the session lane still re-file")


async def _delivered_then(report_delivered=None):
    log = []

    async def dispatch(pool, eid, server, op, args, token_id=None):
        log.append(op)
        if op == "stepup_status":
            return {"exit": 0, "stdout": json.dumps({"state": "delivered"})}
        return {"exit": 0, "stdout": json.dumps({"state": "pending", "request": "NEW"})}
    kw = {} if report_delivered is None else {"report_delivered": report_delivered}
    with _Patch((nac_stepup, "box_stepup_enrolled", _enrolled), (nac_dcn, "_dispatch_box", dispatch)):
        try:
            out = await nac_stepup.box_confirmation(
                object(), 3, server="box1", purpose="op|x", details={"kind": "mgmt_op"},
                existing={"request": "OLD", "server": "box1", "purpose": "op|x"}, **kw)
        except TypeError as e:          # no such keyword: reported as a FAIL, not a crash
            out = {"state": f"TypeError: {e}"}
    return out, log


out, log = asyncio.run(_delivered_then())
check("box_confirmation by default (management ops, session lane): 'delivered' still files a "
      "fresh ask, as before", out.get("state") == "pending" and log == ["stepup_status", "stepup_request"],
      (out, log))
out, log = asyncio.run(_delivered_then(report_delivered=True))
check("with report_delivered=True: 'delivered' is reported and nothing is filed",
      out.get("state") == "delivered" and log == ["stepup_status"], (out, log))


def _box_conf_calls(path):
    """[(enclosing function, report_delivered keyword or None)] for every box_confirmation /
    grant_confirmation call in a module."""
    t = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    out = []
    for fn in ast.walk(t):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("box_confirmation", "grant_confirmation"):
                    kw = {k.arg: k.value for k in node.keywords}
                    v = kw.get("report_delivered")
                    out.append((fn.name, node.func.attr,
                                v.value if isinstance(v, ast.Constant) else (v.id if isinstance(v, ast.Name) else None)))
    return out


_na = _box_conf_calls("server/network/nac_approval.py")
_sl = _box_conf_calls("server/network/session_lane.py")
check("management-op approvals (nac_approval.obtain) keep the default: no report_delivered",
      [c for c in _na if c[0] == "obtain"] and all(c[2] is None for c in _na if c[0] == "obtain"), _na)
check("the session lane keeps the default: no report_delivered",
      _sl and all(c[2] is None for c in _sl), _sl)
check("grant_confirmation passes it through to the console lane",
      ("grant_confirmation", "box_confirmation", "report_delivered") in _na, _na)
_gp = _box_conf_calls("server/network/nac_commit.py") + _box_conf_calls("server/network/service_reach.py")
check("the store, move-plan and reach approve paths pass report_delivered=True",
      sorted(c[:2] for c in _gp if c[2] is True) == [("_approve_dcn_move_plan_tx", "box_confirmation"),
                                                     ("_approve_dcn_store_tx", "grant_confirmation"),
                                                     ("_approve_reach_tx", "grant_confirmation")], _gp)


# ── 25. a discard during the race is never reported approved ──────────────────
print("25. an approval racing a discard ends in 409 with the row still revoked; a mutant dropping "
      "the revoked check is caught")
import inspect   # noqa: E402


async def _race_then_discard(row, approve, patch_target, table):
    db = _Db(row)
    pool = _DbPool(db)
    conf, release, calls = _racing_confirm(_EXPIRED)
    with _Patch(patch_target(conf), (nac_stepup, "verify_stepup", _verify_code)):
        async def attempt():
            try:
                return await approve(pool)
            except Exception as e:
                return e
        loser = asyncio.ensure_future(attempt())
        await asyncio.sleep(0)
        winner = await attempt()                                      # approved ...
        db.apply(f"UPDATE {table} SET revoked_at=now() WHERE id=$1", (db.row["id"],))   # ... then discarded
        release.set()
        late = await loser
    return winner, late, db


def _mutant(module, name):
    """`name` with its revoked check dropped — what the check below must catch."""
    src = inspect.getsource(getattr(module, name))
    assert 'if not row or row["revoked_at"] is not None:' in src, name
    ns = dict(module.__dict__)
    exec(src.replace('if not row or row["revoked_at"] is not None:', "if not row:"), ns)
    return ns[name]


for label, row, approve, target, table, module, reread in (
        ("store", _race_store, lambda p: nac_commit.approve_dcn_store(p, 3, 8, approved_by="user@machine"),
         lambda f: (nac_approval, "grant_confirmation", f), "dcn_stores", nac_commit, "_store_after_race"),
        ("move plan", _race_plan, lambda p: nac_commit.approve_dcn_move_plan(p, 3, 5, approved_by="user@machine"),
         lambda f: (nac_stepup, "box_confirmation", f), "dcn_move_plans", nac_commit, "_plan_after_race"),
        ("reach grant", _race_reach, lambda p: service_reach.approve_reach(p, 3, 9, approved_by="user@machine"),
         lambda f: (nac_approval, "grant_confirmation", f), "service_reach_grants", service_reach,
         "_reach_after_race")):
    w, l, d = asyncio.run(_race_then_discard(row, approve, target, table))
    check(f"{label}: the loser of a race whose winner was then discarded gets 409, not the approval",
          getattr(l, "status", None) == 409 and "while it was being approved" in getattr(l, "detail", ""),
          repr(l))
    check(f"{label}: ...and the row is still revoked", d.row.get("revoked_at") is not None, d.row.get("revoked_at"))
    if not hasattr(module, reread):     # an older tree: nothing to mutate — a FAIL, not a crash
        check(f"{label}: {reread} exists to be mutated", False, "missing")
        continue
    with _Patch((module, reread, _mutant(module, reread))):
        w, l, d = asyncio.run(_race_then_discard(row, approve, target, table))
    check(f"{label}: a mutant {reread} without the revoked check reports the revoked row approved "
          f"(so the check above catches it)", isinstance(l, dict) and l.get("approved") is True, repr(l))


# ── 26. the plan and reach approve writes are guarded like the others ─────────
print("26. a plan or reach grant discarded between the approval's read and its write is refused 409")


async def _discard_mid_approval(row, approve, target, table, error):
    """The machine answers 'approved' — but the row was discarded while it was asking."""
    db = _Db(row)
    pool = _DbPool(db)

    async def conf(*a, existing=None, **kw):
        db.apply(f"UPDATE {table} SET revoked_at=now() WHERE id=$1", (db.row["id"],))
        return {"state": "approved", "approval": "APPROVAL-LATE", "request": existing}
    with _Patch(target(conf)):
        try:
            out = await approve(pool)
        except error as e:
            out = e
    return out, db


for label, row, approve, target, table, error in (
        ("move plan", _race_plan, lambda p: nac_commit.approve_dcn_move_plan(p, 3, 5, approved_by="user@machine"),
         lambda f: (nac_stepup, "box_confirmation", f), "dcn_move_plans", nac_commit.CommitError),
        ("reach grant (defensive: FOR UPDATE holds the row)", _race_reach,
         lambda p: service_reach.approve_reach(p, 3, 9, approved_by="user@machine"),
         lambda f: (nac_approval, "grant_confirmation", f), "service_reach_grants", service_reach.ReachError)):
    out, d = asyncio.run(_discard_mid_approval(row, approve, target, table, error))
    check(f"{label}: the approval is refused 409, never reported granted",
          isinstance(out, error) and out.status == 409, repr(out))
    check(f"{label}: ...and the discarded row was not approved over", d.row.get("approved_at") is None
          and d.row.get("revoked_at") is not None, (d.row.get("approved_at"), d.row.get("revoked_at")))


if _fails:
    print(f"\nFAIL — {len(_fails)} check(s)")
    sys.exit(1)
print("\nPASS")
