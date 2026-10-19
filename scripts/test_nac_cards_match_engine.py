#!/usr/bin/env python3
"""The Network app's NAC cards say what the engine and the gate enforce.

Three defects, each pinned here against the real server code and the real app.js
(run under node, the way test_nac_allow_permits.py runs it):

  1. The ingress engine refuses EVERY paired machine while any approved rule fails its
     integrity check (nac_controller.decide), and nothing told the user: the Ingress
     card went on saying a failing rule "admits paired machines (mcp)". /nac/policy now
     reports the RUNNING engine's own state (engine.tamper, and each approved row's
     verdict as `integrity`), the lane shows a banner saying every paired machine is
     locked out and what to do, and the failing card stops claiming it admits anything.
  2. When the Host cannot read its signing key, every signed rule's removal asks for the
     code (correctly: the check errs toward asking), but the refusal said the rule "keeps
     the machines it matches out" — a claim nothing checked. The list and the refusal now
     say it is the key (revoke_code_reason "key_unreadable"), and still ask.
  3. Management grants expire, and the gate stops applying an expired one (its "do NOT"
     too), but the list kept it as an approved grant whose removal asked for the code, and
     every expiry went through fmtRel, which reads any future time as "just now" ("expires
     just now" on every live grant). An expired grant is now marked by the gate's own rule (column OR signed
     expiry), revokes free on server and card alike, shows its terms as what it WAS, and
     the confirm says what removing a live grant, an expired one, or a draft does.

Stub pool standing in for the database (it applies each query's filters), a real
ed25519 key, the real NACController, policy_list, revoke_policy, mgmt_grants_list,
revoke_mgmt_grant and load_mgmt_grants. No pod, no DB, no network.

Run: APP_ROOT=<repo> python3 scripts/test_nac_cards_match_engine.py
"""
import asyncio
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import types

ROOT = os.environ.get("APP_ROOT") or os.environ.get("CSERIES_ROOT") or \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey   # noqa: E402

from server.network import nac_commit, nac_mgmt, nac_stepup                      # noqa: E402
from server.network.nac_controller import NACController, _canonical              # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{str(extra)[:600]}]" if not cond and extra != "" else ""))
    if not cond:
        _fails.append(name)


_KEY = Ed25519PrivateKey.generate()          # stands in for the entity key
_PUB = _KEY.public_key().public_bytes_raw()
_NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
_APPROVED = (_NOW - dt.timedelta(days=2)).isoformat()
_MCP = [{"direction": "inbound", "lane": "mcp"}]


def _sig(terms):
    return base64.b64encode(_KEY.sign(_canonical(terms))).decode()


# ── a stub database: one list per table, each query answered by its own filters ──────────
class _Db:
    def __init__(self):
        self.profiles, self.policy, self.grants = [], [], []
        self.fail_engine_load = False

    # the pool and its connection are the same object here
    def acquire(self):
        db = self

        class _A:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *a):
                return False
        return _A()

    def transaction(self):
        class _T:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False
        return _T()

    @staticmethod
    def _col_expired(g):
        e = g.get("expires_at")
        return e is not None and dt.datetime.fromisoformat(e) <= dt.datetime.now(dt.timezone.utc)

    def _grant_row(self, g, q=""):
        # col_expired exists only when the query selects it, as in the database: a read that
        # forgets _GRANT_COL_EXPIRED_SQL must not be handed the answer by the stub.
        return dict(g, col_expired=self._col_expired(g)) if "AS col_expired" in q else dict(g)

    async def fetch(self, q, *a):
        q1 = " ".join(q.split())
        if "FROM nac_profiles" in q1:
            return [dict(p) for p in self.profiles if p.get("revoked_at") is None]
        if "FROM nac_policy" in q1:
            rows = [dict(r) for r in self.policy if r.get("revoked_at") is None]
            if "approved_at IS NOT NULL" in q1:
                if self.fail_engine_load:
                    raise RuntimeError("engine load failed")
                rows = [r for r in rows if r.get("approved_at")]
            return rows
        if "FROM nac_mgmt_grants" in q1:
            rows = [self._grant_row(g, q1) for g in self.grants if g.get("revoked_at") is None]
            if "approved_at IS NOT NULL" in q1:
                rows = [r for r in rows if r.get("approved_at")]
            if "expires_at > now()" in q1:
                rows = [r for r in rows if not self._col_expired(r)]
            return rows
        return []

    async def fetchrow(self, q, *a):
        q1 = " ".join(q.split())
        if "FROM nac_policy p LEFT JOIN nac_profiles" in q1:
            r = next((r for r in self.policy if r["id"] == a[0] and r.get("revoked_at") is None), None)
            if r is None:
                return None
            p = next((p for p in self.profiles if p["id"] == r["profile_id"]
                      and p.get("revoked_at") is None), None)
            return dict(r, prof_id=p["id"] if p else None, prof_name=p["name"] if p else None,
                        prof_enforcement=p["enforcement"] if p else None,
                        prof_permits=p["permits"] if p else None)
        if "FROM nac_mgmt_grants" in q1:
            g = next((g for g in self.grants if g["id"] == a[0] and g.get("revoked_at") is None), None)
            return self._grant_row(g, q1) if g else None
        return None   # nac_endpoints: not reported yet -> posture unknown

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        q1 = " ".join(q.split())
        table = self.policy if "UPDATE nac_policy" in q1 else (
            self.grants if "UPDATE nac_mgmt_grants" in q1 else None)
        if table is not None and "revoked_at=now()" in q1:
            for r in table:
                if r["id"] == a[0] and r.get("revoked_at") is None:
                    if "approved_at IS NULL" in q1 and r.get("approved_at"):
                        return "UPDATE 0"
                    r["revoked_at"] = _NOW.isoformat()
                    return "UPDATE 1"
            return "UPDATE 0"
        return "UPDATE 0"


def _profile(pid, name, enforcement, permits, revoked=False):
    return {"id": pid, "name": name, "enforcement": enforcement, "is_default": False,
            "permits": json.dumps(permits), "revoked_at": _NOW.isoformat() if revoked else None}


def _rule(rid, name, enforcement, permits, *, pid, approved=True, signed=True, tamper=False, match=None):
    terms = {"name": name, "priority": rid * 10, "match": match or {},
             "enforcement": enforcement, "permits": permits}
    sig = _sig(terms)
    if tamper:   # stored terms changed after signing: the signature no longer verifies
        terms = dict(terms, permits=[{"direction": "inbound", "lane": "any"}])
    on = approved and signed
    return {"id": rid, "name": name, "priority": rid * 10, "match": json.dumps(match or {}),
            "profile_id": pid, "proposed_by": "host",
            "approved_at": _APPROVED if approved else None, "approved_by": "user", "stepup_at": None,
            "signed_terms": json.dumps(terms) if on else None, "signature": sig if on else None,
            "revoked_at": None}


# ── the key lookups, the step-up, and the live engine behind server.main ───────────────────
_key_state = {"readable": True}


async def _entity_pub(pool, eid):
    if not _key_state["readable"]:
        raise RuntimeError("entity key unavailable")
    return _PUB


async def _verify_code(pool, code):
    return code == "123456"


async def _not_enrolled(pool):
    return True


nac_stepup.entity_verify_pubkey = _entity_pub
nac_stepup.verify_stepup = _verify_code
nac_stepup.is_enrolled = _not_enrolled

_live = {"nac": None}
_main = types.ModuleType("server.main")
_main.get_nac_controller = lambda: _live["nac"]
sys.modules["server.main"] = _main


def _engine(db, *, armed=True):
    """The engine as server.main builds it: pool-bound, armed by attach_pool from the entity key."""
    nac = NACController(pool=db, entity_id=9003 if armed else None)
    asyncio.run(nac.attach_pool(db))
    _live["nac"] = nac
    return nac


def _list(db):
    return json.loads(json.dumps(asyncio.run(nac_commit.policy_list(db, 3)), default=str))


def _by_id(listed, rid):
    return next((r for r in listed["policy"] if r["id"] == rid), None)


# ── the real app.js, under node ────────────────────────────────────────────────────────────
_JS = open(os.path.join(ROOT, "system-apps/network/app.js"), encoding="utf-8").read()


def _slice(start, end):
    i = _JS.find(start)
    j = _JS.find(end, i + 1)
    if i < 0 or j < 0:
        raise SystemExit(f"FAIL — app.js marker missing: {start!r} .. {end!r}")
    return _JS[i:j]


_nd = _JS.find("function nacDispatch(")
_APP = "\n".join([
    _slice("const $ = (id)", "// ── lens switching"),
    _slice("async function loadNAC()", "// Second-factor chip"),
    _slice("// ── INGRESS —", "// ── v125: ADD RULE"),
    _slice("// A grant in one line", "// ── MANAGEMENT AUDIT"),
    _slice("const LANES = [", "function toggleLane("),
    _JS[_JS.find("function declineGrant(id)"):_JS.find("\n    }\n", _nd) + 7],
])

_HARNESS = r"""
const els = {};
function mkEl(id) {
  if (!els[id]) {
    const s = new Set();
    els[id] = { id, innerHTML: '', textContent: '', hidden: false,
      classList: { toggle(c, on) { if (on === undefined) on = !s.has(c); on ? s.add(c) : s.delete(c); },
                   contains(c) { return s.has(c); }, list() { return [...s]; } },
      setAttribute() {}, removeAttribute() {}, querySelector() { return null; }, style: {} };
  }
  return els[id];
}
const document = { getElementById: mkEl, createElement() {
  let t = ''; return { set textContent(v) { t = String(v); },
    get innerHTML() { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }; } };
const state = { nac: { laneOpen: {} }, loaded: {} };
const confirms = [], posts = [];
let confirmAnswer = true;
function confirm(m) { confirms.push(m); return confirmAnswer; }
function alert(m) { confirms.push('ALERT ' + m); }
function withStepup(fn, widening) { posts.push({ widening: !!widening }); fn(widening ? '123456' : null); }
function nacReload() {}
function renderNacStepupChip() {} function renderNacPending() {} function renderNacPosture() {}
function renderNacEgress() {} function renderNacAudit() {} function updateNacBadge() {}
"""


def _node(inputs, body):
    """Run the app slices under node with INPUTS (a JSON-able dict) bound as `inputs`,
    then BODY; returns what BODY prints as JSON, or (None, error)."""
    js = (_HARNESS + _APP + "\nfetchJSON = async (url, opts) => {"
          " if (opts && opts.method === 'POST') { posts.push({ url, body: opts.body }); return {}; }"
          " const r = inputs.reads[url.replace('/api/network', '')]; return r === undefined ? {} : r; };\n"
          + "const inputs = " + json.dumps(inputs, default=str) + ";\n"
          + "(async () => {\n" + body + "\n})().catch(e => { console.error(e && e.stack || e); process.exit(3); });\n")
    try:
        p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return None, "node is not installed; the card checks cannot run"
    if p.returncode != 0:
        return None, p.stderr.strip()[-800:]
    try:
        return json.loads(p.stdout.strip().splitlines()[-1]), ""
    except Exception as e:
        return None, f"{e}: {p.stdout[-400:]}"


def _ingress_view(listed):
    """loadNAC over the real /nac/policy answer, then what the Ingress lane shows."""
    return _node({"reads": {"/nac/policy": listed}}, """
        await loadNAC();
        console.log(JSON.stringify({ html: els['nac-ingress-list'].innerHTML,
            count: els['nac-ingress-count'].textContent,
            countLocked: els['nac-ingress-count'].classList.contains('locked'),
            open: els['nac-lane-ingress'].classList.contains('open') }));""")


def _card(html, name):
    """The one rule card whose name is NAME (the markup between its name and the next card)."""
    i = html.find(f'<span class="net-card-name">{name}</span>')
    if i < 0:
        return ""
    j = html.find('<div class="net-card ', i)
    return html[i: j if j > 0 else len(html)]


# ══ 1. the ingress lock is shown, from the running engine ════════════════════════════════
print("1. an approved rule failing its integrity check: the engine locks every machine out, and the lane says so")
db = _Db()
db.profiles = [_profile(5, "home", "allow", _MCP), _profile(6, "office", "allow", _MCP)]
db.policy = [_rule(1, "home", "allow", _MCP, pid=5)]
nac = _engine(db)
listed = _list(db)
check("intact: the running engine is reported, armed and not locked",
      listed.get("engine") == {"running": True, "tamper": False, "armed": True}, listed.get("engine"))
check("intact: the approved rule's verdict is the engine's 'ok'", _by_id(listed, 1).get("integrity") == "ok",
      _by_id(listed, 1))
view, err = _ingress_view(listed)
check("the Ingress lane renders under node", view is not None, err)
if view:
    check("intact: no lock banner, and the card says what it admits",
          "nac-lock-banner" not in view["html"] and "admits paired machines (mcp)" in view["html"], view["html"][:600])
    check("intact: the lane count is the rule count, and the lane stays closed",
          view["count"] == "1" and not view["open"], view)

db.policy.append(_rule(2, "office", "allow", _MCP, pid=6, tamper=True))
listed = _list(db)
check("a signed rule changed after approval: engine.tamper is the running engine's flag",
      listed.get("engine", {}).get("tamper") is True and nac._tamper is True, listed.get("engine"))
ev = asyncio.run(nac.evaluate(42))
check("...and it is enforced: the gate's own evaluate() refuses a machine the intact rule admits",
      ev.get("allowed") is False and "integrity" in ev.get("why", ""), ev)
r2, r1 = _by_id(listed, 2), _by_id(listed, 1)
check("the failing row: integrity 'failed', reason 'signature'",
      r2.get("integrity") == "failed" and r2.get("integrity_reason") == "signature", r2)
check("the intact row stays 'ok' (only the rejected rule is marked)", r1.get("integrity") == "ok", r1)
check("the failing row's removal asks for the code, because it keeps machines out",
      r2.get("revoke_needs_code") is True and r2.get("revoke_code_reason") == "keeps_out", r2)
view, err = _ingress_view(listed)
check("locked: the lane renders", view is not None, err)
if view:
    h = view["html"]
    check("locked: the banner says every paired machine with a device fingerprint is locked out",
          "nac-lock-banner" in h and "Every paired machine with a device fingerprint is locked out." in h, h[:700])
    check("locked: it names the rule and says it failed its signature check",
          "“office” failed its signature check" in h, h[:700])
    check("locked: it says what to do — revoke it (asking for the code) and draft it again",
          "Revoke it below (it asks for your code)" in h and "draft it again and approve the new draft" in h, h[:900])
    fc = _card(h, "office")
    check("the failing card is marked, and no longer claims it admits anything",
          "signature fails" in fc and "admits nothing: its signature does not verify" in fc
          and "admits every lane" not in fc and "admits paired machines" not in fc, fc)
    check("the failing card's Revoke asks for the code", 'data-widen="1"' in fc, fc)
    ic = _card(h, "home")
    check("the intact card does not claim it admits while the engine refuses everything",
          "admits nothing while machines are locked out" in ic and "· admits paired machines (mcp) ·" not in ic, ic)
    check("locked: the lane count reads 'locked' and the lane opens itself",
          view["count"] == "locked" and view["countLocked"] and view["open"], view)

db.profiles.append(_profile(7, "legacy", "allow", _MCP, revoked=True))
db.policy.append(_rule(3, "legacy", "allow", _MCP, pid=7, signed=False))
listed = _list(db)
r3 = _by_id(listed, 3)
check("an unsigned approved rule whose profile is gone: 'failed' with reason 'profile'",
      r3.get("integrity") == "failed" and r3.get("integrity_reason") == "profile", r3)
view, err = _ingress_view(listed)
if view:
    check("two failing rules: the banner names both and asks for the code on each",
          "“office”, “legacy”" in view["html"] and "each asks for your code" in view["html"], view["html"][:900])
    check("the profile-less card says its profile is gone",
          "profile missing" in _card(view["html"], "legacy")
          and "the profile it points at is gone" in _card(view["html"], "legacy"), _card(view["html"], "legacy"))

# Removing the failing rules lifts the lock at the next read, and the engine agrees.
for rid in (2, 3):
    out = asyncio.run(nac_commit.revoke_policy(db, 3, rid, stepup_code="123456"))
listed = _list(db)
check("after the failing rules are revoked, the next read reports the lock lifted",
      listed["engine"]["tamper"] is False and all(r.get("integrity") == "ok" for r in listed["policy"]),
      listed)
check("...and the engine itself admits again (the same snapshot the read reported)",
      asyncio.run(nac.evaluate(42)).get("allowed") is True)
view, err = _ingress_view(listed)
if view:
    check("...and the banner is gone", "nac-lock-banner" not in view["html"], view["html"][:400])

# What the engine could not check is never reported as a failure.
print("   'could not check' is never shown as 'failed'")
db2 = _Db()
db2.profiles = [_profile(5, "home", "allow", _MCP)]
db2.policy = [_rule(1, "home", "allow", _MCP, pid=5, tamper=True),
              _rule(4, "draft", "allow", _MCP, pid=5, approved=False)]
_live["nac"] = None
listed = _list(db2)
check("no engine running: engine reported as not running, tamper unknown (None), rows 'unchecked'",
      listed["engine"] == {"running": False, "tamper": None, "armed": None}
      and _by_id(listed, 1)["integrity"] == "unchecked", listed)
check("a draft's integrity is None", _by_id(listed, 4)["integrity"] is None, _by_id(listed, 4))
v, err = _ingress_view(listed)
check("no engine: no banner and no failure mark", v is not None and "nac-lock-banner" not in v["html"]
      and "signature fails" not in v["html"], err or (v or {}).get("html", "")[:400])
unarmed = _engine(db2, armed=False)
listed = _list(db2)
check("an unarmed engine: armed False, not locked, and a signed row is 'unchecked' (never 'ok' or 'failed')",
      listed["engine"] == {"running": True, "tamper": False, "armed": False}
      and _by_id(listed, 1)["integrity"] == "unchecked", listed)
_engine(db2)
db2.fail_engine_load = True
listed = _list(db2)
check("an engine that cannot load its rules: tamper unknown (None), rows 'unchecked'",
      listed["engine"] == {"running": True, "tamper": None, "armed": None}
      and _by_id(listed, 1)["integrity"] == "unchecked", listed)
db2.fail_engine_load = False

# The Host's own readers carry the engine's state too.
print("   the Host's nac_status and get_network carry it")
_engine(db)
db.policy.append(_rule(8, "tampered", "allow", _MCP, pid=6, tamper=True))
snap = asyncio.run(nac_commit.status_snapshot(db, 3))
check("nac_status (status_snapshot) reports policy.engine.tamper and the failing row",
      snap.get("policy", {}).get("engine", {}).get("tamper") is True
      and any(r.get("integrity") == "failed" for r in snap["policy"].get("policy", [])), snap.get("policy"))
try:
    from server import brain_service
    from mastercode.runtime import tools
    _gp = brain_service.get_pool
    brain_service.get_pool = lambda: db
    try:
        gn = asyncio.run(tools._exec_get_network({}))
    finally:
        brain_service.get_pool = _gp
    gp = (gn.get("result") or {}).get("nac_policy") or {}
    check("get_network's nac_policy carries the engine's state",
          (gp.get("engine") or {}).get("tamper") is True, gp.get("engine"))
except Exception as e:   # the tool must be importable here, so an import error is a failure
    check("get_network's nac_policy carries the engine's state", False, repr(e))
_skill = " ".join(open(os.path.join(ROOT, "default-skills/network.md"), encoding="utf-8").read().split())
check("skill: nac_status's policy.engine.tamper is explained, with the way out",
      "`policy.engine.tamper` true means an approved ingress rule failed its integrity check" in _skill
      and "revoking that rule (it needs their code)" in _skill, "")
db.policy = [r for r in db.policy if r["id"] != 8]


# ══ 2. the unreadable key is named as the reason, not the rule ═══════════════════════════
print("2. when the key cannot be read, the code is still asked for, and the reason is the key")
db3 = _Db()
db3.profiles = [_profile(5, "home", "allow", _MCP), _profile(6, "shut", "quarantine", [])]
db3.policy = [_rule(1, "home", "allow", _MCP, pid=5), _rule(2, "shut", "quarantine", [], pid=6)]
_engine(db3)                                   # the engine armed its key at startup
_key_state["readable"] = False                 # ...and this request cannot read it
try:
    listed = _list(db3)
    h1 = _by_id(listed, 1)
    check("the engine still verifies the rule ('ok', not locked)",
          h1.get("integrity") == "ok" and listed["engine"]["tamper"] is False, (h1, listed["engine"]))
    check("its removal still asks for the code, and says the reason is the key",
          h1.get("revoke_needs_code") is True and h1.get("revoke_code_reason") == "key_unreadable", h1)
    try:
        asyncio.run(nac_commit.revoke_policy(db3, 3, 1))
        err3 = None
    except nac_commit.CommitError as e:
        err3 = e
    check("revoke without the code is refused 403, naming the key",
          err3 is not None and err3.status == 403 and "can't read its signing key" in err3.detail, repr(err3))
    check("...and it does not claim the rule keeps machines out",
          err3 is not None and "keeps the machines" not in err3.detail, repr(err3))
    check("...and still says 'step-up code', so the app's retry prompt fires on it",
          err3 is not None and "step-up code" in err3.detail, repr(err3))
    v, err = _ingress_view(listed)
    if v:
        c = _card(v["html"], "home")
        check("the card says the key is why it asks, and shows no failure",
              "can&#39;t read its signing key right now" in c or "can't read its signing key right now" in c, c)
        check("...no lock banner and no 'signature fails'",
              "nac-lock-banner" not in v["html"] and "signature fails" not in c, c)
    out = asyncio.run(nac_commit.revoke_policy(db3, 3, 1, stepup_code="123456"))
    check("with the code it is removed", out.get("revoked") is True, out)
finally:
    _key_state["readable"] = True
listed = _list(db3)
check("with the key readable, a quarantine's reason is 'keeps_out'",
      _by_id(listed, 2).get("revoke_code_reason") == "keeps_out", _by_id(listed, 2))
try:
    asyncio.run(nac_commit.revoke_policy(db3, 3, 2))
    err4 = None
except nac_commit.CommitError as e:
    err4 = e
check("...and its refusal still says it keeps the machines out",
      err4 is not None and "keeps the machines it matches out" in err4.detail, repr(err4))


# ══ 3. expired management grants ══════════════════════════════════════════════════════════
print("3. an expired management grant: the gate skips it, so the card says so and removal is free")


def _grant(gid, server, ops, deny, *, expires, col_expires="same", approved=True):
    terms = {"entity": 3, "server": server, "ops": ops, "deny": deny, "intent": None,
             "expires": expires.isoformat()}
    col = expires if col_expires == "same" else col_expires
    return {"id": gid, "server": server, "ops": json.dumps(ops), "deny": json.dumps(deny),
            "intent": None, "note": None, "proposed_by": "host",
            "approved_at": _APPROVED if approved else None, "approved_by": "user" if approved else None,
            "stepup_at": None, "expires_at": col.isoformat() if (approved and col) else None,
            "signed_terms": json.dumps(terms) if approved else None,
            "signature": _sig(terms) if approved else None, "revoked_at": None}


_future = _NOW + dt.timedelta(days=12, hours=1)
_past = _NOW - dt.timedelta(days=3, hours=1)
db4 = _Db()
db4.grants = [
    _grant(1, "live.box", ["status"], ["rm"], expires=_future),
    _grant(2, "old.box", ["status"], ["rm"], expires=_past),
    _grant(3, "colpast.box", [], ["rm"], expires=_future, col_expires=_past),   # a column write
    _grant(4, "sigpast.box", [], ["rm"], expires=_past, col_expires=_future),   # column says live
    _grant(5, "draft.box", ["status"], [], expires=_future, approved=False),
]
gl = json.loads(json.dumps(asyncio.run(nac_commit.mgmt_grants_list(db4, 3)), default=str))
g = {x["id"]: x for x in gl["grants"]}
loaded = {x["server"] for x in asyncio.run(nac_mgmt.load_mgmt_grants(db4, _PUB))}
check("the gate loads only the live grant (column OR signed expiry skips the rest)",
      loaded == {"live.box"}, loaded)
check("the list's `expired` is exactly the gate's skip, for every approved grant",
      all(g[i]["expired"] == (g[i]["server"] not in loaded) for i in (1, 2, 3, 4)),
      {i: g[i]["expired"] for i in g})
check("a draft is not 'expired'", g[5]["expired"] is False, g[5])
check("revoke_needs_code: only a live grant carrying a 'do NOT' asks",
      [g[i]["revoke_needs_code"] for i in (1, 2, 3, 4, 5)] == [True, False, False, False, False],
      [g[i]["revoke_needs_code"] for i in (1, 2, 3, 4, 5)])
check("a free revoke never widens: every grant whose 'do NOT' the gate applies still asks",
      all(nac_commit.grant_revoke_widens(db4._grant_row(x, "AS col_expired")) for x in db4.grants
          if x["server"] in loaded and json.loads(x["deny"])), "")
check("the list sends neither the signed terms nor the helper column",
      all("signed_terms" not in x and "col_expired" not in x for x in gl["grants"]), sorted(g[1]))
for gid in (2, 3, 4):
    try:
        out = asyncio.run(nac_commit.revoke_mgmt_grant(db4, 3, gid))
    except nac_commit.CommitError as e:
        out = {"refused": e.status, "detail": e.detail}
    check(f"revoking expired grant {gid} ({g[gid]['server']}) needs no code",
          out.get("revoked") is True and out.get("stepped_up") is False, out)
try:
    asyncio.run(nac_commit.revoke_mgmt_grant(db4, 3, 1))
    err5 = None
except nac_commit.CommitError as e:
    err5 = e
check("revoking the live grant carrying a 'do NOT' still needs the code",
      err5 is not None and err5.status == 403, repr(err5))

print("   the grant cards and the confirm")
db4b = _Db()
db4b.grants = [_grant(1, "live.box", ["status"], ["rm"], expires=_future),
               _grant(2, "old.box", ["status"], ["rm"], expires=_past),
               _grant(6, "plain.box", ["status"], [], expires=_future),
               _grant(5, "draft.box", ["status"], [], expires=_future, approved=False)]
gl = json.loads(json.dumps(asyncio.run(nac_commit.mgmt_grants_list(db4b, 3)), default=str))
v, err = _node({"reads": {"/nac/mgmt/grants": gl}, "future": _future.isoformat(), "past": _past.isoformat(),
               "skewed": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
               "justpast": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=20)).isoformat()}, """
    state.nac.grants = inputs.reads['/nac/mgmt/grants'].grants;
    state.nac.suspended = [];
    renderNacGrants({});
    const html = els['nac-grants-list'].innerHTML;
    state.nac.grants = [Object.assign({}, inputs.reads['/nac/mgmt/grants'].grants[0],
        { expires_at: inputs.justpast, expired: false })];
    renderNacGrants({});
    const skew = els['nac-grants-list'].innerHTML;
    state.nac.grants = inputs.reads['/nac/mgmt/grants'].grants;
    renderNacGrants({});
    const out = { skew, html, rel: [fmtRel(inputs.future), fmtRel(inputs.past), fmtRel(inputs.skewed)],
                  exp: [fmtExpiry(inputs.future), fmtExpiry(inputs.past), fmtExpiry(null)] };
    confirms.length = 0; posts.length = 0;
    nacDispatch('grant', 'decline', '5', false);
    nacDispatch('grant', 'revoke', '2', false);
    nacDispatch('grant', 'revoke', '6', false);
    nacDispatch('grant', 'revoke', '1', true);
    out.confirms = confirms.slice();
    out.posts = posts.slice();
    console.log(JSON.stringify(out));""")
check("the grant cards render under node", v is not None, err)
if v:
    check("fmtRel stays past-tense: a past event reads '3d ago', and a time ahead of the browser "
          "(clock skew on an 'approved' stamp) reads 'just now', never 'in 5m'",
          v["rel"] == ["just now", "3d ago", "just now"], v["rel"])
    check("fmtExpiry: 'expires in 12d', 'expired 3d ago', 'no expiry'",
          v["exp"] == ["expires in 12d", "expired 3d ago", "no expiry"], v["exp"])
    h = v["html"]
    live = h[h.find("live.box"):h.find("old.box")]
    old = h[h.find("old.box"):h.find("plain.box")]
    check("a live grant says when it expires, not 'just now' (the line and the card's sub-line)",
          live.count("expires in 12d") == 2 and "just now" not in live, live)
    # The server's `expired` decides both lines of a card, so a grant the gate still loads
    # never reads "expires …" on one line and "expired …" on the other (browser clock ahead).
    st = v["skew"]
    check("one card, one answer: a grant the server calls live reads as live on both lines",
          st.count("expires in under a minute") == 2 and "expired" not in st, st)
    check("the grant line names its ops once",
          "live.box · ops [ status ] · never [ rm ] · expires in 12d" in live, live)
    check("a live grant's 'do NOT' is shown as holding, and its Revoke asks for the code",
          "· never [ rm ]" in live and 'data-widen="1"' in live, live)
    check("an expired grant says so and that it no longer applies",
          "expired 3d ago — no longer applies" in old and ">expired<" in old, old)
    check("an expired grant's 'do NOT' is shown as what it was, not as holding",
          "was never [ rm ]" in old and "· never [ rm ]" not in old, old)
    check("an expired grant's Revoke is free", 'data-widen="0"' in old, old)
    c = v["confirms"]
    check("confirm, declining a draft: it was never in force, the host loses nothing",
          len(c) > 0 and "never in force" in c[0] and "loses nothing" in c[0], c)
    check("confirm, removing an expired grant: it stopped applying, the host loses nothing",
          len(c) > 1 and "expired" in c[1] and "loses nothing" in c[1] and "loses these rights" not in c[1], c)
    check("confirm, revoking a live grant: the host loses these rights",
          len(c) > 2 and "loses these rights immediately" in c[2], c)
    check("confirm, revoking a live grant with a 'do NOT': it asks for the code",
          len(c) > 3 and "asks for your code" in c[3], c)
    check("each removal was sent, and only the live 'do NOT' grant went through the code prompt",
          [p.get("widening") for p in v["posts"] if "widening" in p] == [False, False, False, True], v["posts"])


print()
if _fails:
    print(f"FAIL — {len(_fails)} check(s)")
    for f in _fails:
        print("  - " + f)
    sys.exit(1)
print("OK — the NAC cards say what the engine and the gate enforce")
