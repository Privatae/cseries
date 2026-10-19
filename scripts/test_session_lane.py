#!/usr/bin/env python3
"""Gate: terminal sessions on a bastion (host half) hold their walls.

Pure Python — a fake pool, a fake worker socket, a real Ed25519 key. No DB, no
container, no network. Runs under pytest or as a script (both exit non-zero on
a failure).

What it pins:
  1. FRAMING — open sends ONE 'S' frame with the spec the box expects; O bytes come
     back as events; resize sends 'R'; keyboard sends 'I'; X ends it and the audit
     row says closed with that exit.
  2. GRANT REUSE — signed_terms is jsonb, so the bytes in the S frame are the
     platform's own _canonical(terms) re-run on the stored dict (nac_stepup.py) and
     the stored signature verifies over exactly those bytes. Nothing is re-signed.
  3. THE HOST GATE runs before any S frame: a
     revoked row, an expired signed expiry, a deny-only grant covering the server, a
     suspended or quarantined server, or no grant at all → refused on the host in the
     126 shape, no worker touched, audited denied-*.
  4. ROOT needs a step-up approval id; without one it is refused before the box.
  5. LANES — the hello parser strips ` lane=session`; session workers live in their
     own pool; an S never takes a harness worker and an H never takes a session worker.
  6. THE TOOL — op='run' caps output at 64 KB, types command + exit, refuses root by
     construction; registered where mgmt_run is.
  7. THE APP — manifest loads, scopes are real, no CDN, no host/port field.

Run: python3 scripts/test_session_lane.py   |   pytest scripts/test_session_lane.py
"""
from __future__ import annotations

import ast
import asyncio
import base64
import datetime as _dt
import json
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

from server.network import nac_harness, nac_mgmt, nac_stepup, session_lane  # noqa: E402
from server.network.nac_harness import HarnessRelay, HarnessWorker, parse_harness_hello  # noqa: E402

# One loop for the whole file. The workers and sessions take the current loop at
# construction (as they do inside the running server); outside a server there is none
# until we make one.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

SERVER = "box-a"
TOKEN_ID = 7
EID = 1


# ── fakes ──────────────────────────────────────────────────────────────────────

class FakePool:
    """Just enough asyncpg: grant rows filtered the way the real WHERE clause filters
    them (approved, not revoked, not expired), the heartbeat token lookup, the suspend
    and quarantine tables, and every execute() captured (audit rows included)."""

    def __init__(self, grants=None, token_id=TOKEN_ID):
        self.grants = list(grants or [])
        self.token_id = token_id
        self.suspended: set = set()
        self.quarantined: set = set()
        self.executed: list = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetch(self, sql, *args):
        if "FROM nac_mgmt_grants" in sql:
            assert "revoked_at IS NULL" in sql and "approved_at IS NOT NULL" in sql, sql
            now = _dt.datetime.now(_dt.timezone.utc)
            return [r for r in self.grants
                    if r.get("approved_at") is not None and r.get("revoked_at") is None
                    and (r.get("expires_at") is None or r["expires_at"] > now)]
        if "FROM nac_bastion_suspend" in sql:
            return [{"server": s} for s in self.suspended]
        if "FROM nac_bastion_quarantine" in sql:
            return [{"server": s} for s in self.quarantined]
        return []

    async def fetchval(self, sql, *args):
        if "FROM network_bastion_heartbeat" in sql and "token_id" in sql:
            return self.token_id if args and args[0] == SERVER else None
        return None

    def audits(self):
        return [a for sql, a in self.executed if sql.startswith("INSERT INTO nac_mgmt_audit")]


class FakeWS:
    """The bastion's side of one worker socket, as the endpoint sees it."""

    def __init__(self):
        self.sent: list = []
        self.inbound: "asyncio.Queue" = asyncio.Queue()
        self._idle = asyncio.get_event_loop().create_future()

    async def send_bytes(self, b):
        self.sent.append(bytes(b))

    async def receive_bytes(self):
        v = await self.inbound.get()
        if isinstance(v, Exception):
            raise v
        return v

    async def receive(self):
        # the idle-worker watch in serve(); never resolves unless the box drops
        return await self._idle

    def box_sends(self, tag: bytes, payload: bytes = b""):
        self.inbound.put_nowait(tag + payload)


KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def signed_row(ops, *, server=SERVER, deny=(), expires_days=30, revoked=False, gid=1):
    """A nac_mgmt_grants row the way approve_mgmt_grant writes it (nac_commit.py:178-186):
    terms {entity, server, ops, deny, intent, expires}, signature over _canonical(terms).
    The stored dict is SHUFFLED to stand in for jsonb's key reordering."""
    expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=expires_days)).replace(microsecond=0)
    terms = {"entity": EID, "server": server, "ops": list(ops), "deny": list(deny),
             "intent": None, "expires": expires.isoformat()}
    sig = base64.b64encode(KEY.sign(nac_stepup._canonical(terms))).decode()
    shuffled = {k: terms[k] for k in ("ops", "expires", "server", "deny", "entity", "intent")}
    return {"id": gid, "server": server, "ops": list(ops), "signed_terms": shuffled, "signature": sig,
            "expires_at": expires, "approved_at": _dt.datetime.now(_dt.timezone.utc),
            "revoked_at": _dt.datetime.now(_dt.timezone.utc) if revoked else None}


async def _pubkey(pool, eid):
    return PUB


def _fresh_relay(monkeypatch_like=None):
    relay = HarnessRelay()
    nac_harness._relay = relay
    return relay


def _run(coro):
    return LOOP.run_until_complete(coro)


def _setup():
    nac_stepup.entity_verify_pubkey = _pubkey  # the host key, without a DB


# ── 1 + 2: framing and grant reuse ────────────────────────────────────────────

def test_framing_and_grant_reuse():
    _setup()
    pool = FakePool([signed_row(["uptime", "session:open"])])
    relay = _fresh_relay()
    ws = FakeWS()
    worker = HarnessWorker(ws, TOKEN_ID, lane="session")
    serve = asyncio.ensure_future(worker.serve(relay))

    async def go():
        s = await session_lane.open_session(pool, EID, SERVER, "session:open", cols=100, rows=30)
        await asyncio.sleep(0.01)
        assert len(ws.sent) == 1 and ws.sent[0][:1] == b"S", ws.sent
        spec = json.loads(ws.sent[0][1:])
        assert spec["op"] == "session:open" and spec["cols"] == 100 and spec["rows"] == 30
        assert spec["shell"] == "" and spec["approval"] == ""
        assert session_lane.valid_session_id(spec["session_id"]) and spec["session_id"] == s.session_id
        # grant reuse: the bytes are the platform's canonical form of the STORED dict,
        # and the STORED signature verifies over exactly those bytes
        terms_bytes = base64.b64decode(spec["grant_terms_b64"])
        stored = pool.grants[0]
        assert terms_bytes == nac_stepup._canonical(stored["signed_terms"])
        assert json.loads(terms_bytes) == {"entity": EID, "server": SERVER, "ops": ["uptime", "session:open"],
                                           "deny": [], "intent": None, "expires": stored["signed_terms"]["expires"]}
        assert spec["grant_sig_b64"] == stored["signature"]
        KEY.public_key().verify(base64.b64decode(spec["grant_sig_b64"]), terms_bytes)  # raises if not
        # VERIFY_PUBKEY: the field the gateway pins the host key from, so a
        # Terminal needs no DCN folder grant first. Nothing here used to
        # assert the round trip — the framing test caught the raw-bytes catastrophe that broke
        # every terminal, but nothing pinned the CONTENT or the ENCODING, which
        # is the half the box actually parses.
        #
        # entity_verify_pubkey returns RAW 32 bytes; serialising those directly
        # is unencodable and produced NO S frame at all. Standard base64 of
        # exactly 32 bytes, matching client-go/session.go, which
        # StdEncoding-decodes and requires len 32.
        vp = spec["verify_pubkey"]
        assert isinstance(vp, str) and vp, f"verify_pubkey is {vp!r}"
        raw = base64.b64decode(vp, validate=True)
        assert len(raw) == 32, f"verify_pubkey decodes to {len(raw)} bytes, not 32"
        assert raw == PUB, "verify_pubkey is not THIS host's key"
        # box -> host bytes
        ws.box_sends(b"O", b"$ ")
        assert await asyncio.wait_for(s.recv(), 1) == ("O", b"$ ")
        # host -> box keyboard + resize
        s.write(b"ls\n")
        s.resize(120, 40)
        await asyncio.sleep(0.01)
        assert ws.sent[1] == b"Ils\n"
        assert ws.sent[2][:1] == b"R" and json.loads(ws.sent[2][1:]) == {"cols": 120, "rows": 40}
        # a notice, then exit
        ws.box_sends(b"E", b"idle for 30 minutes\n")
        assert await asyncio.wait_for(s.recv(), 1) == ("E", "idle for 30 minutes")
        ws.box_sends(b"X", b"124")
        assert await asyncio.wait_for(s.recv(), 1) == ("X", 124)
        assert await asyncio.wait_for(s.recv(), 1) is None
        assert s.exit == 124 and s.closed
        await asyncio.sleep(0.02)
        auds = pool.audits()
        assert (EID, SERVER, "session:open", "opened", None) in auds, auds
        assert (EID, SERVER, "session:open", "closed", 124) in auds, auds
        assert s.user and "root" not in s.user
    _run(go())
    _run(asyncio.wait_for(serve, 1))


# ── 3: the host gate, before any S ────────────────────────────────────────────

def _refused(pool, op="session:open", **kw):
    relay = _fresh_relay()
    ws = FakeWS()
    worker = HarnessWorker(ws, TOKEN_ID, lane="session")
    serve = asyncio.ensure_future(worker.serve(relay))

    async def go():
        s = await session_lane.open_session(pool, EID, SERVER, op, **kw)
        ev1 = await asyncio.wait_for(s.recv(), 1)
        ev2 = await asyncio.wait_for(s.recv(), 1)
        assert ev1[0] == "E" and ev2 == ("X", 126), (ev1, ev2)
        assert await s.recv() is None
        assert ws.sent == [], "a refused session must never reach the box"
        assert relay.worker_count(TOKEN_ID, "session") == 1, "the idle worker stays idle"
        return ev1[1]
    reason = _run(go())
    serve.cancel()
    try:
        _run(serve)
    except BaseException:
        pass
    return reason


def _opened(pool, op="session:open", **kw):
    """Open a session that the pod lets through; returns the S frame the box saw."""
    relay = _fresh_relay()
    ws = FakeWS()
    worker = HarnessWorker(ws, TOKEN_ID, lane="session")
    serve = asyncio.ensure_future(worker.serve(relay))

    async def go():
        s = await session_lane.open_session(pool, EID, SERVER, op, **kw)
        await asyncio.sleep(0.01)
        assert len(ws.sent) == 1 and ws.sent[0][:1] == b"S", ws.sent
        spec = json.loads(ws.sent[0][1:])
        ws.box_sends(b"X", b"0")
        await s.wait(1)
        return spec
    spec = _run(go())
    _run(asyncio.wait_for(serve, 1))
    return spec


# A SHELL IS THE PERSON'S OWN ACT: the
# pod no longer refuses one for want of a Host grant listing the session op.
# The machine's own checks are the authority (bastion 0.39.0). A grant that
# lists the op still rides the frame — a box older than 0.39.0 requires it —
# and one that does not (none, revoked, expired, another server, tampered)
# is simply not carried.

def test_no_grant_opens_with_no_grant_in_the_frame():
    _setup()
    pool = FakePool([])
    spec = _opened(pool)
    assert spec["op"] == "session:open" and spec["grant_terms_b64"] == "" and spec["grant_sig_b64"] == ""
    assert spec["verify_pubkey"], "the host key still rides the frame, so the box can pin it"
    assert (EID, SERVER, "session:open", "opened", None) in pool.audits()


def test_a_grant_that_does_not_cover_the_op_is_not_carried():
    _setup()
    for row in (signed_row(["session:open"], revoked=True),
                signed_row(["session:open"], server="box-b"),
                signed_row(["uptime"])):
        spec = _opened(FakePool([row]))
        assert spec["grant_terms_b64"] == "" and spec["grant_sig_b64"] == "", row
    expired = signed_row(["session:open"], expires_days=-1)
    expired["expires_at"] = None  # column says never; the SIGNED expiry is what counts
    assert _opened(FakePool([expired]))["grant_terms_b64"] == ""
    tampered = signed_row(["uptime"])
    tampered["signed_terms"]["ops"] = ["uptime", "session:open"]  # widened after signing
    assert _opened(FakePool([tampered]))["grant_terms_b64"] == ""


def test_deny_only_grant_beats_allow():
    _setup()
    pool = FakePool([signed_row(["session:open"], gid=1),
                     signed_row([], deny=["session:open"], gid=2)])
    reason = _refused(pool)
    assert "denied by your grant" in reason
    assert (EID, SERVER, "session:open", "denied-user", 126) in pool.audits()


def test_deny_all_on_the_server_refuses():
    _setup()
    pool = FakePool([signed_row(["session:open"], gid=1), signed_row([], deny=["*"], gid=2)])
    _refused(pool)


def test_a_deny_refuses_even_with_no_grant_at_all():
    _setup()
    pool = FakePool([signed_row([], deny=["session:open"])])
    reason = _refused(pool)
    assert "denied by your grant" in reason


def test_wildcard_server_grant_opens():
    _setup()
    pool = FakePool([signed_row(["session:open"], server="*")])
    g = _run(session_lane.signed_grant_for(pool, PUB, SERVER, "session:open"))
    assert g is not None and g["terms"]["server"] == "*"
    assert _opened(pool)["grant_terms_b64"], "a grant that lists the op still rides the frame"


def test_suspended_and_quarantined_refuse():
    _setup()
    pool = FakePool([])
    pool.suspended = {SERVER}
    assert "suspended" in _refused(pool)
    pool = FakePool([])
    pool.quarantined = {SERVER}
    assert "quarantined" in _refused(pool)


def test_no_verify_key_denies_everything():
    _setup()
    pool = FakePool([signed_row(["session:open"])])
    assert _run(session_lane.signed_grant_for(pool, None, SERVER, "session:open")) is None


# ── 4: root ───────────────────────────────────────────────────────────────────

def test_root_gate_runs_before_the_machine_is_asked():
    """Filing the step-up before the host gate would let any write:network
    caller ring the user's second factor for a server with no session:root grant."""
    _setup()
    calls = []

    async def fake_confirmation(pool, eid, *, server, purpose, details, existing=None):
        calls.append((server, purpose, details))
        return {"state": "pending", "request": {"request": "req-1", "server": server, "purpose": purpose}}
    real = nac_stepup.box_confirmation
    nac_stepup.box_confirmation = fake_confirmation
    try:
        # a DENY of session:root → refused on the host, the machine never hears of it
        pool = FakePool([signed_row([], deny=["session:root"])])
        st = _run(session_lane.begin_root(pool, EID, SERVER, "abcdefgh12"))
        assert st["state"] == "refused" and "not permitted" in st["detail"]
        assert calls == []
        assert (EID, SERVER, "session:root", "denied-user", 126) in pool.audits()
        # suspended → the same, even with the grant
        pool = FakePool([signed_row(["session:root"])])
        pool.suspended = {SERVER}
        assert _run(session_lane.begin_root(pool, EID, SERVER, "abcdefgh12"))["state"] == "refused"
        assert calls == []
        # no grant at all (a shell is the person's own act) → the request is filed
        pool = FakePool([])
        st = _run(session_lane.begin_root(pool, EID, SERVER, "abcdefgh12", note="why"))
        assert st["state"] == "pending" and st["request"]["request"] == "req-1"
        assert calls == [(SERVER, "session_root|box-a|abcdefgh12",
                          {"kind": "session_root", "server": SERVER, "session_id": "abcdefgh12", "note": "why"})]
        # the route's first step for EITHER op is session_lane.begin (the gate
        # first — begin_root for root, host_gate then the write-gate approval for
        # open; bastion 0.33.0), before anything is said, and poll only after.
        src = open(os.path.join(ROOT, "server", "routes", "network.py")).read()
        seg = src[src.index("async def session_ws"):src.index("# ── P8.1 management grants")]
        assert seg.index("session_lane.begin(") < seg.index("session_lane.poll(")
        assert seg.index("session_lane.begin(") < seg.index('"t": "stepup"')
        assert "request_root_approval(" not in seg and "begin_root(" not in seg
    finally:
        nac_stepup.box_confirmation = real


def test_live_sessions_list_and_close():
    _setup()
    session_lane._LIVE.clear()
    pool = FakePool([signed_row(["session:open"])])
    relay = _fresh_relay()
    ws = FakeWS()
    worker = HarnessWorker(ws, TOKEN_ID, lane="session")
    serve = asyncio.ensure_future(worker.serve(relay))

    async def go():
        assert session_lane.list_live() == []
        s = await session_lane.open_session(pool, EID, SERVER, "session:open", origin="host-tool")
        live = session_lane.list_live()
        assert len(live) == 1 and live[0]["session_id"] == s.session_id
        assert live[0]["server"] == SERVER and live[0]["origin"] == "host-tool" and live[0]["op"] == "session:open"
        assert live[0]["started_at"] == s.started and live[0]["last_activity"]
        assert session_lane.close_live("nope") is False
        assert session_lane.close_live(s.session_id) is True   # the user closes the host's shell
        await asyncio.sleep(0.02)
        assert s.closed and session_lane.list_live() == []
        assert session_lane.close_live(s.session_id) is False
    _run(go())
    _run(asyncio.wait_for(serve, 1))
    src = open(os.path.join(ROOT, "server", "routes", "network.py")).read()
    assert '@router.get("/sessions", dependencies=[Depends(requires_permission("read:network"))])' in src
    assert '@router.post("/sessions/{session_id}/close", dependencies=[Depends(requires_permission("write:network"))])' in src


def test_root_without_approval_is_refused_before_the_box():
    _setup()
    pool = FakePool([signed_row(["session:open", "session:root"])])
    reason = _refused(pool, op="session:root")
    assert "approval" in reason
    assert (EID, SERVER, "session:root", "denied-pod", 126) in pool.audits()


def test_root_with_approval_sends_it_and_needs_the_root_op():
    _setup()
    # a deny of session:root → root refused, approval or not
    _refused(FakePool([signed_row([], deny=["session:root"])]), op="session:root", approval="appr-1")
    # no grant → S carries the approval and no grant; the box's code check is the authority
    pool = FakePool([])
    relay = _fresh_relay()
    ws = FakeWS()
    worker = HarnessWorker(ws, TOKEN_ID, lane="session")
    serve = asyncio.ensure_future(worker.serve(relay))

    async def go():
        s = await session_lane.open_session(pool, EID, SERVER, "session:root", approval="appr-1")
        await asyncio.sleep(0.01)
        spec = json.loads(ws.sent[0][1:])
        assert spec["op"] == "session:root" and spec["approval"] == "appr-1"
        assert spec["grant_terms_b64"] == "" and spec["grant_sig_b64"] == ""
        assert s.user == "root"
        ws.box_sends(b"X", b"0")
        await s.wait(1)
    _run(go())
    _run(asyncio.wait_for(serve, 1))


def test_root_purpose_matches_the_box():
    assert session_lane.root_purpose("box-a", "abc12345") == "session_root|box-a|abc12345"
    d = session_lane.root_request_details("box-a", "abc12345", "why")
    assert d == {"kind": "session_root", "server": "box-a", "session_id": "abc12345", "note": "why"}


# ── 5: lanes ──────────────────────────────────────────────────────────────────

def test_hello_parsing():
    assert parse_harness_hello("BEARER abc") == ("abc", "harness")
    assert parse_harness_hello("BEARER abc lane=session") == ("abc", "session")
    assert parse_harness_hello("BEARER abc lane=harness") == ("abc", "harness")
    assert parse_harness_hello("BEARER abc lane=bogus") is None
    assert parse_harness_hello("BEARER abc extra") is None, "unknown trailing words are refused, not ignored"
    assert parse_harness_hello("BEARER abc lane=session x=1") is None
    assert parse_harness_hello("BEARER") is None
    assert parse_harness_hello("HELLO abc") is None
    assert parse_harness_hello("") is None
    # the endpoint hashes ONLY the bearer, and closes 4401 on a None
    src = open(os.path.join(ROOT, "server", "main.py")).read()
    seg = src[src.index('@app.websocket("/bastion-harness")'):]
    seg = seg[:seg.index('@app.websocket("/bastion-embed")')]
    assert "parse_harness_hello(hello)" in seg and "hashlib.sha256(bearer.encode())" in seg
    assert "safe_close(4401)" in seg[:seg.index("bearer, lane = parsed")]
    assert "lane=lane" in seg


def test_session_pool_is_separate_from_harness_pool():
    _setup()
    relay = _fresh_relay()
    hws, sws = FakeWS(), FakeWS()
    h = HarnessWorker(hws, TOKEN_ID, lane="harness")
    s = HarnessWorker(sws, TOKEN_ID, lane="session")
    t1 = asyncio.ensure_future(h.serve(relay))
    t2 = asyncio.ensure_future(s.serve(relay))

    async def go():
        await asyncio.sleep(0.01)
        assert relay.worker_count(TOKEN_ID, "harness") == 1
        assert relay.worker_count(TOKEN_ID, "session") == 1
        assert relay.worker_count(TOKEN_ID) == 1  # default = harness, as run_harness_op calls it
        # an H job never takes the session worker: with no harness worker idle it times out
        relay._workers[(TOKEN_ID, "harness")].discard(h)
        relay._idle[(TOKEN_ID, "harness")].get_nowait()
        assert await relay.dispatch(TOKEN_ID, {"harness_id": "x"}, timeout=0.05) is None
        assert sws.sent == []
        # and an S never takes a harness worker: no session worker → 502, no frame anywhere
        relay._workers[(TOKEN_ID, "session")].discard(s)
        relay._idle[(TOKEN_ID, "session")].get_nowait()
        relay._register_idle(h)
        pool = FakePool([signed_row(["session:open"])])
        try:
            await session_lane.open_session(pool, EID, SERVER, "session:open")
            assert False, "expected MgmtError"
        except nac_mgmt.MgmtError as e:
            assert e.status == 502 and "session worker" in e.detail
        assert hws.sent == []
        assert (EID, SERVER, "session:open", "no-worker", None) in pool.audits()
    _run(go())
    for t in (t1, t2):
        t.cancel()
        try:
            _run(t)
        except BaseException:
            pass


# ── 6: there is no host tool ────────────────────────────────────────────────
# No AI on the machines: the
# host's `session` tool — op='run', a command through a shell the box opened on
# itself — is GONE, with run_command and its caps. The lane itself stays: it is
# the Terminal app's, the person's own act, root approved on the machine.

def test_the_host_holds_no_shell_tool():
    from mastercode import public_tool_registry as reg
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    assert "session" not in reg.OPERATOR_PLATFORM_TOOL_SCHEMAS, "the host's session tool is gone"
    assert "session" not in reg.PUBLIC_META_TOOL_NAMES
    assert "session" not in META_TOOL_CAPS
    assert not hasattr(session_lane, "run_command"), "the tool's op='run' left with it"
    assert not hasattr(session_lane, "RUN_TIMEOUT_MAX_S") and not hasattr(session_lane, "RUN_OUTPUT_CAP")
    tools_src = open(os.path.join(ROOT, "brain", "mastercode", "runtime", "tools.py")).read()
    assert 'elif name == "session":' not in tools_src and "_exec_session" not in tools_src
    tree = ast.parse(tools_src)
    assert not any(isinstance(n, ast.AsyncFunctionDef) and n.name == "_exec_session" for n in ast.walk(tree))
    cov = open(os.path.join(ROOT, "scripts", "check_capability_coverage.py")).read()
    assert '"session": ("operator_only"' not in cov
    direct = open(os.path.join(ROOT, "scripts", "check_direct_ops.py")).read()
    assert '"session",' in direct, "retired, so a revival can never land as a direct op"
    skill = open(os.path.join(ROOT, "default-skills", "network.md")).read()
    m = re.search(r"^allowed-tools:\s*(.*)$", skill, re.M)
    assert m and "session" not in [t.strip() for t in m.group(1).split(",")]
    assert "## session — guidance" not in skill and "## There is no shell — guidance" in skill
    # The app's lane is untouched: open_session, the root gate, the registry.
    for name in ("open_session", "begin_root", "request_root_approval", "list_live", "SESSION_OPS"):
        assert hasattr(session_lane, name), name


# ── 7: the app ────────────────────────────────────────────────────────────────

def test_terminal_app_manifest_and_assets():
    from server.applications import manifest
    from server.applications.scopes import PERMISSIONS
    from pathlib import Path
    app_dir = os.path.join(ROOT, "system-apps", "terminal")
    m = manifest.load(Path(app_dir) / "app.yaml")
    assert m.id == "terminal" and m.system is True and m.provenance == "standard"
    assert set(m.permissions) == {"read:topology", "read:network", "write:network"}
    assert set(m.permissions) <= set(PERMISSIONS)
    assert m.prompt_behavior is not None and "root" in m.prompt_behavior.step_up
    for f in ("index.html", "app.js", "style.css", "icon.svg", "vendor/xterm.js", "vendor/xterm.css",
              "vendor/addon-fit.js", "vendor/LICENSE-xterm"):
        assert os.path.isfile(os.path.join(app_dir, f)), f
    html = open(os.path.join(app_dir, "index.html")).read()
    assert not re.search(r'(src|href)\s*=\s*["\']\s*(https?:)?//', html), "no CDN — every asset is vendored"
    assert "vendor/xterm.js" in html and "vendor/addon-fit.js" in html
    assert not re.search(r'<input[^>]*(host|port)', html, re.I), "no host/port field: the target is a bastion"
    js = open(os.path.join(app_dir, "app.js")).read()
    assert "/api/network/bastions" in js and "/api/network/sessions/" in js and "TICKET " in js
    assert "session:root" in js and "126" in js and "tmux" in js
    assert "lastNotice" in js and "/api/network/sessions')" in js and "/close`" in js
    assert "needs no standing grant from this Host" in html  # a shell is the person's own act
    assert "ssh://" not in js and "new SSH" not in js


# ── script mode ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, "—", repr(e))
    sys.exit(1 if fails else 0)
