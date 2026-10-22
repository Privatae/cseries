#!/usr/bin/env python3
"""A Posture card says what the Host's engine does with that machine now.

The card printed nac_endpoints.assigned_profile, falling back to "default-preserve".
Nothing writes that column (migrations/v058_nac_engine.sql says the PDP does; no code
does), so every card read "default-preserve" — under a quarantine rule that matched the
machine, and while the engine was refusing every machine because an approved rule failed
its integrity check. /nac/endpoints now carries each machine's decision from the running
engine (nac_commit.endpoint_decisions: decide() over the row, then permits_inbound on the
mcp lane — the call the MCP gate makes), and the card shows it.

Pinned here: the route and the real app.js card agree with the gate's own evaluate() for
every machine, in every state (no rules, a quarantine rule, the default profile, the
integrity lock, no engine at all), and a bearer-only token is shown as not governed.
With no engine the gate refuses every governed machine, so the card says refused and
that the rules did not load.

Stub pool (it answers each query from in-memory rows), a real ed25519 key, the real
NACController, the real route, the real app.js under node. No pod, no DB, no network.

Run: APP_ROOT=<repo> python3 scripts/test_nac_posture_card_decision.py
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import types

ROOT = os.environ.get("APP_ROOT") or os.environ.get("CSERIES_ROOT") or \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey   # noqa: E402

from server import brain_service                                                  # noqa: E402
from server.network import nac_stepup                                             # noqa: E402
from server.network.nac_controller import NACController, _canonical              # noqa: E402
from server.routes import network as net_routes                                  # noqa: E402
from server.routes.mcp import _nac_gate                                          # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{str(extra)[:700]}]" if not cond and extra != "" else ""))
    if not cond:
        _fails.append(name)


_KEY = Ed25519PrivateKey.generate()
_PUB = _KEY.public_key().public_bytes_raw()
_MCP = [{"direction": "inbound", "lane": "mcp"}]


class _Db:
    """Profiles, policy, endpoints and tokens; each query answered by its own filters.

    The route's query is answered by its own projection: bound_fingerprint and
    token_expired are taken from whichever table the SELECT names, so a query that reads
    the endpoint's fingerprint where it means the token's gets the endpoint's value here."""
    def __init__(self):
        self.profiles, self.policy, self.endpoints = [], [], []
        self.tokens = {}   # token_id -> {"hardware_fingerprint", "expired"}
        self.quarantined = []   # hostnames under a manual (whole-mesh) quarantine

    def _route_row(self, e, q1):
        t = self.tokens[e["token_id"]]
        row = {k: e[k] for k in ("token_id", "hostname", "classification", "posture",
                                 "posture_at", "decided_at", "last_decision", "fingerprint")}
        if "t.hardware_fingerprint AS bound_fingerprint" in q1:
            row["bound_fingerprint"] = t["hardware_fingerprint"]
        elif "e.fingerprint AS bound_fingerprint" in q1:
            row["bound_fingerprint"] = e["fingerprint"]
        if "t.expires_at <= now()) AS token_expired" in q1:
            row["token_expired"] = t["expired"]
        return row

    async def fetch(self, q, *a):
        q1 = " ".join(q.split())
        if "FROM nac_profiles" in q1:
            return [dict(p) for p in self.profiles]
        if "FROM nac_policy" in q1:
            return [dict(r) for r in self.policy if "approved_at IS NOT NULL" not in q1 or r["approved_at"]]
        if "FROM nac_endpoints e" in q1:
            # the route's query: the endpoint row joined to its live bastion token
            return [self._route_row(e, q1) for e in self.endpoints]
        if "FROM nac_bastion_quarantine" in q1:
            return [{"server": h} for h in self.quarantined]
        return []   # suspend set: none

    async def fetchrow(self, q, *a):
        q1 = " ".join(q.split())
        if "FROM nac_endpoints WHERE token_id" in q1:   # the gate's own read (_endpoint_state)
            e = next((e for e in self.endpoints if e["token_id"] == a[0]), None)
            return None if e is None else {k: e[k] for k in ("token_id", "fingerprint",
                                                                "classification", "posture")}
        return None


def _profile(pid, name, enforcement, permits, default=False):
    return {"id": pid, "name": name, "enforcement": enforcement, "is_default": default,
            "permits": json.dumps(permits)}


def _rule(rid, name, enforcement, permits, *, pid, match=None, tamper=False):
    terms = {"name": name, "priority": rid * 10, "match": match or {},
             "enforcement": enforcement, "permits": permits}
    sig = base64.b64encode(_KEY.sign(_canonical(terms))).decode()
    if tamper:
        terms = dict(terms, permits=[{"direction": "inbound", "lane": "any"}])
    return {"id": rid, "name": name, "priority": rid * 10, "match": json.dumps(match or {}),
            "profile_id": pid, "approved_at": "2026-09-27T00:00:00+00:00",
            "signed_terms": json.dumps(terms), "signature": sig}


def _endpoint(db, tid, host, posture, *, bound=True, ep_fp=None, expired=False):
    """An endpoint row and its token. The endpoint's own fingerprint (what decide() matches
    on) and the token's bound one (what the gate's exemption reads) are kept apart."""
    db.tokens[tid] = {"hardware_fingerprint": f"fp-{tid}" if bound else None, "expired": expired}
    return {"token_id": tid, "hostname": host, "classification": "server", "posture": posture,
            "posture_at": "2026-09-28T00:00:00+00:00", "decided_at": None, "last_decision": None,
            "fingerprint": ep_fp if ep_fp is not None else f"fp-{tid}"}


async def _entity_pub(pool, eid):
    return _PUB


nac_stepup.entity_verify_pubkey = _entity_pub

_live = {"nac": None}
_main = types.ModuleType("server.main")
_main.get_nac_controller = lambda: _live["nac"]
sys.modules["server.main"] = _main


def _engine(db):
    nac = NACController(pool=db, entity_id=9003)
    asyncio.run(nac.attach_pool(db))
    _live["nac"] = nac
    return nac


def _route(db):
    brain_service.get_pool = lambda: db
    return json.loads(json.dumps(asyncio.run(net_routes.nac_endpoints_list()), default=str))


# ── the real card, under node ────────────────────────────────────────────────────────────
_JS = open(os.path.join(ROOT, "system-apps/network/app.js"), encoding="utf-8").read()


def _slice(start, end):
    i = _JS.find(start)
    j = _JS.find(end, i + 1)
    if i < 0 or j < 0:
        raise SystemExit(f"FAIL — app.js marker missing: {start!r} .. {end!r}")
    return _JS[i:j]


_APP = "\n".join([
    _slice("const $ = (id)", "// ── lens switching"),
    _slice("async function loadNAC()", "// Second-factor chip"),
    _slice("// What the Host does with this machine now", "// ── INGRESS —"),
])
_HARNESS = r"""
const els = {};
function mkEl(id) { if (!els[id]) els[id] = { id, innerHTML: '', textContent: '',
  classList: { toggle() {}, contains() { return false; } }, setAttribute() {}, style: {} }; return els[id]; }
const document = { getElementById: mkEl, createElement() { let t = ''; return {
  set textContent(v) { t = String(v); },
  get innerHTML() { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }; } };
const state = { nac: { laneOpen: {} }, loaded: {} };
function renderNacStepupChip() {} function renderNacPending() {} function renderNacIngress() {}
function renderNacEgress() {} function renderNacGrants() {} function renderNacAudit() {}
function applyLaneDefaults() {} function updateNacBadge() {}
"""


def _cards(listed):
    js = (_HARNESS + _APP + "\nfetchJSON = async (url) => url.endsWith('/nac/endpoints') ? inputs : {};\n"
          + "const inputs = " + json.dumps(listed) + ";\n"
          + "(async () => { await loadNAC(); console.log(JSON.stringify(els['nac-posture-grid'].innerHTML)); })()"
          + ".catch(e => { console.error(e && e.stack || e); process.exit(3); });\n")
    try:
        p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return None, "node is not installed"
    if p.returncode != 0:
        return None, p.stderr.strip()[-600:]
    return json.loads(p.stdout.strip().splitlines()[-1]), ""


def _card(html, host):
    i = html.find(f'<span class="nac-host-name">{host}</span>')
    if i < 0:
        return ""
    j = html.find('<div class="nac-posture-card', i)
    return html[i: j if j > 0 else len(html)]


def _agrees_with_gate(db, listed):
    """Every card's admitted/refused is what the real MCP gate (_nac_gate) does with that
    token, fed the token's own bound fingerprint the way the bearer check hands it over.
    An expired token never reaches _nac_gate (the bearer check refuses it), so refused."""
    out = []
    for e in listed["endpoints"]:
        d = e.get("decision") or {}
        t = db.tokens[e["token_id"]]
        if t["expired"]:
            gate = False
        else:
            auth = {"id": e["token_id"], "hardware_fingerprint": t["hardware_fingerprint"]}
            gate = asyncio.run(_nac_gate(auth)) is None
        out.append((e["hostname"], d.get("allowed", True if d.get("governed") is False else None), gate))
    return all(a == b for _h, a, b in out) and bool(out), out


# ── 1. no rules: admitted by default ─────────────────────────────────────────────────────
print("1. no rules: every governed machine is admitted by default, a bearer-only one is not governed")
db = _Db()
db.endpoints = [_endpoint(db, 11, "desk", "healthy"), _endpoint(db, 12, "nas", "unknown"),
                _endpoint(db, 13, "pasted", "healthy", bound=False, ep_fp="fp-stale")]
nac = _engine(db)
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("the route carries the engine's decision per machine",
      d["desk"] == {"governed": True, "allowed": True, "profile": None, "locked": False,
                    "default": False, "why": "default-preserve: no NAC policy authored"}, d["desk"])
check("a token with no bound fingerprint is not governed, even though its endpoint row still "
      "carries an old fingerprint (the gate reads the token's)",
      d["pasted"] == {"governed": False}, d["pasted"])
check("the route no longer sends the fingerprints, the expiry flag or assigned_profile",
      all(k not in listed["endpoints"][0]
          for k in ("fingerprint", "bound_fingerprint", "token_expired", "assigned_profile")),
      sorted(listed["endpoints"][0]))
ok, got = _agrees_with_gate(db, listed)
check("every card's admitted/refused equals what the real MCP gate does with that token", ok, got)
html, err = _cards(listed)
check("the Posture cards render under node", html is not None, err)
if html:
    check("no rules: the card says admitted by default, and never prints default-preserve",
          "none (admitted by default)" in _card(html, "desk") and ">admitted<" in _card(html, "desk")
          and "default-preserve" not in html, _card(html, "desk"))
    check("bearer-only: the card says your Ingress rules skip it",
          "your Ingress rules skip it" in _card(html, "pasted"), _card(html, "pasted"))

# ── 2. a quarantine rule that matches ────────────────────────────────────────────────────
print("2. a quarantine rule for unknown posture: that machine is refused, the healthy one admitted")
db.profiles = [_profile(5, "healthy-in", "allow", _MCP), _profile(6, "shut-unknown", "quarantine", [])]
db.policy = [_rule(1, "shut-unknown", "quarantine", [], pid=6, match={"posture": ["unknown"]}),
             _rule(2, "healthy-in", "allow", _MCP, pid=5, match={"posture": ["healthy"]})]
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("the unknown-posture machine is refused under the quarantine rule",
      d["nas"].get("allowed") is False and d["nas"].get("profile") == "shut-unknown", d["nas"])
check("the healthy machine is admitted under the allow rule",
      d["desk"].get("allowed") is True and d["desk"].get("profile") == "healthy-in", d["desk"])
ok, got = _agrees_with_gate(db, listed)
check("...and both agree with the gate", ok, got)
html, err = _cards(listed)
if html:
    nas = _card(html, "nas")
    check("the refused card names the rule and says the Host refuses it",
          "shut-unknown" in nas and ">refused<" in nas
          and "Your Host refuses it: the rule “shut-unknown” does not admit it." in nas, nas)

# ── 3. the default profile ───────────────────────────────────────────────────────────────
print("3. no rule matches and a default profile decides")
db.profiles.append(_profile(7, "catch-quarantine", "quarantine", [], default=True))
db.policy = [_rule(2, "healthy-in", "allow", _MCP, pid=5, match={"posture": ["healthy"]})]
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("the unmatched machine is decided by the default profile, marked as the default",
      d["nas"].get("default") is True and d["nas"].get("profile") == "catch-quarantine" and d["nas"].get("allowed") is False,
      d["nas"])
html, err = _cards(listed)
if html:
    check("the card says no rule matches and the default profile does not admit it",
          "no rule matches it, and the default profile “catch-quarantine” does not admit it." in _card(html, "nas"),
          _card(html, "nas"))
db.profiles.pop()

# ── 4. the integrity lock ────────────────────────────────────────────────────────────────
print("4. an approved rule fails its integrity check: every governed machine is refused, and each card says why")
db.policy = [_rule(2, "healthy-in", "allow", _MCP, pid=5, match={"posture": ["healthy"]}),
             _rule(3, "broken", "allow", _MCP, pid=5, tamper=True)]
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("the engine is locked, and the route says so",
      listed.get("engine") == {"running": True, "tamper": True}, listed.get("engine"))
check("every governed machine is refused under the lock, the healthy one included",
      all(d[h].get("allowed") is False and d[h].get("locked") is True and d[h].get("profile") is None
          for h in ("desk", "nas")), d)
ok, got = _agrees_with_gate(db, listed)
check("...and the gate refuses each of them too", ok, got)
html, err = _cards(listed)
if html:
    desk = _card(html, "desk")
    check("the healthy card no longer reads as trusted: it shows the lock and why",
          "integrity lock" in desk and ">refused<" in desk
          and "an approved Ingress rule failed its integrity check" in desk
          and "Trusted with its assigned access" not in desk and "default-preserve" not in desk, desk)
    check("the bearer-only card is unchanged (the gate never asks the engine about it)",
          "your Ingress rules skip it" in _card(html, "pasted"), _card(html, "pasted"))

# ── 4b. a fingerprint rule decides on the endpoint's fingerprint ─────────────────────────
print("4b. a rule matching a fingerprint decides on the endpoint's own fingerprint, as the gate does")
db.policy = [_rule(4, "shut-fp", "quarantine", [], pid=6, match={"fingerprint": "fp-endpoint-only"})]
db.endpoints.append(_endpoint(db, 14, "split", "healthy", ep_fp="fp-endpoint-only"))
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("the machine whose ENDPOINT fingerprint the rule names is refused (its token's differs)",
      d["split"].get("allowed") is False and d["split"].get("profile") == "shut-fp", d["split"])
ok, got = _agrees_with_gate(db, listed)
check("...and the gate agrees for every machine", ok, got)

# ── 4c. a rule named like the lock does not fake the lock ────────────────────────────────
print("4c. a quarantine rule named '__integrity__' is a rule, not the lock")
db.profiles.append(_profile(8, "__integrity__", "quarantine", []))
db.policy = [_rule(5, "__integrity__", "quarantine", [], pid=8, match={"posture": ["unknown"]})]
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("the engine is not locked, and the card's decision says so",
      listed["engine"]["tamper"] is False and d["nas"].get("locked") is False
      and d["nas"].get("profile") == "__integrity__" and d["nas"].get("allowed") is False, (listed["engine"], d["nas"]))
db.profiles.pop()

# ── 4d. a malformed approved match: it fails its check and locks, it never admits ────────
print("4d. a malformed approved match: the rule fails its check, so every governed machine is refused")
db.policy = [_rule(6, "bad-match", "quarantine", [], pid=6, match={"min_posture": ["healthy"]})]
try:
    listed = _route(db)
    crashed = None
except Exception as e:
    listed, crashed = None, e
check("/nac/endpoints does not fail on a rule the engine cannot evaluate", crashed is None, repr(crashed))
if listed:
    d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
    check("the engine is locked by the rule, not admitting through an error",
          listed["engine"]["tamper"] is True and d["desk"].get("locked") is True
          and d["desk"].get("allowed") is False and not d["desk"].get("error"), (listed["engine"], d["desk"]))
    ok, got = _agrees_with_gate(db, listed)
    check("...and the gate refuses them too", ok, got)
    html, err = _cards(listed)
    if html:
        check("the card shows the lock and that the machine is refused",
              "integrity lock" in _card(html, "desk") and ">refused<" in _card(html, "desk"),
              _card(html, "desk"))
    check("the Quarantine buttons are still drawn", html is not None and "data-nac-quarantine" in (html or ""), err)

# ── 4d2. an engine that raises while deciding: the card says refused, as the gate does ───
print("4d2. an engine that raises while deciding: refused, and the card says so")
db.policy = []
_engine(db)
_live["nac"].decide = lambda ep: (_ for _ in ()).throw(TypeError("invented engine fault"))
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("each governed machine is reported refused with an engine error (the gate refuses on one)",
      d["desk"].get("error") is True and d["desk"].get("allowed") is False, d["desk"])
ok, got = _agrees_with_gate(db, listed)
check("...and the gate does refuse them", ok, got)
html, err = _cards(listed)
if html:
    check("the card says the rules could not be applied and the machine is refused",
          "could not be applied" in _card(html, "desk") and ">refused<" in _card(html, "desk"),
          _card(html, "desk"))
_engine(db)

# ── 4e. an expired pairing token ─────────────────────────────────────────────────────────
print("4e. an expired pairing token is refused before any rule")
db.policy = []
db.endpoints.append(_endpoint(db, 15, "lapsed", "healthy", expired=True))
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("the lapsed machine is refused with expired set, though no rule exists",
      d["lapsed"].get("expired") is True and d["lapsed"].get("allowed") is False, d["lapsed"])
ok, got = _agrees_with_gate(db, listed)
check("...and every other card still matches the gate", ok, got)
html, err = _cards(listed)
if html:
    check("the card says its pairing token has expired",
          "pairing token has expired" in _card(html, "lapsed") and ">refused<" in _card(html, "lapsed"),
          _card(html, "lapsed"))
db.endpoints = [e for e in db.endpoints if e["hostname"] not in ("split", "lapsed")]

# ── 4f. a manually quarantined machine ───────────────────────────────────────────────────
print("4f. a manually quarantined machine reads cut off, never admitted")
db.quarantined = ["desk"]
listed = _route(db)
html, err = _cards(listed)
if html:
    desk = _card(html, "desk")
    check("the quarantined card says cut off, and not admitted",
          "cut off (quarantined)" in desk and ">admitted<" not in desk and "Release" in desk, desk)
    check("...while an unquarantined card still shows the engine's answer",
          ">admitted<" in _card(html, "nas"), _card(html, "nas"))
db.quarantined = []

# ── 5. no engine: the gate refuses every governed machine, and the card says so ──────────
print("5. with no engine running, a governed machine's card says refused, as the gate does")
_live["nac"] = None
listed = _route(db)
d = {e["hostname"]: (e.get("decision") or {}) for e in listed["endpoints"]}
check("no engine: engine reported as not running, each governed machine refused as unloaded",
      listed["engine"] == {"running": False, "tamper": None}
      and all(d[h].get("governed") is True and d[h].get("allowed") is False
              and d[h].get("unloaded") is True for h in ("desk", "nas")), (listed["engine"], d))
check("no engine: a bearer-only token is still not governed (the gate exempts it)",
      d["pasted"] == {"governed": False}, d["pasted"])
ok, got = _agrees_with_gate(db, listed)
check("...and every card agrees with the gate, which refuses with no engine", ok, got)
html, err = _cards(listed)
if html:
    desk = _card(html, "desk")
    check("the card says refused and that the rules did not load, never admitted or 'not checked'",
          ">refused<" in desk and "did not load" in desk and "admitted" not in desk
          and "not checked" not in desk and "default-preserve" not in html, desk)
    check("the bearer-only card still says your Ingress rules skip it",
          "your Ingress rules skip it" in _card(html, "pasted"), _card(html, "pasted"))

print()
if _fails:
    print(f"FAIL — {len(_fails)} check(s)")
    for f in _fails:
        print("  - " + f)
    sys.exit(1)
print("OK — each Posture card says what the Host's engine does with that machine")
