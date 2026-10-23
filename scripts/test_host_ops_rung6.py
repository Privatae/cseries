#!/usr/bin/env python3
"""test_host_ops_rung6.py — host-authored ops: the
pod's half of bastion 0.37.0: an op the Host wrote runs on a machine only under
terms this Host signed, after a dry run in the machine's sandbox, with the
person's approval bound to the change list that dry run showed.

Pins, container-free (stub pool, stub relay, a fixed signing key, the real code):

  1. the op directory digest is the box's (ops.go opDirDigest), FIXED VECTOR
     shared with the Go test; the bundle carries the two modes the box loads,
     leaves test/ behind, refuses links and oversize;
  2. the two purposes are the box's, byte for byte;
  3. op_dry_run: the op is signed, synced (3 args) and dry-run (2 args); the
     terms are op_run / one use / a nonce / ≤ 24 h / canonical params; the
     report is kept with its changes_sha256; nothing lands;
  4. op_apply: refused without a live dry run; the approval is asked for on
     the device with the op_run details — kind, sha256, params, expires,
     changes_sha256 — under the purpose the box recomputes; pending is a
     pending result and nothing is dispatched; approved → the apply carries
     the approval, the dry run's nonce and its change list; a box refusal
     comes back as the box's words plus "dry-run again";
  5. op_grant: a root op is refused (never reusable); a user op's grant is
     approved once on the device and installed with op_sync; a later apply
     runs under it through op_grant_run with no tap;
  6. run_mgmt_op hands the three door ops to nac_ops AFTER the pod gate, and
     the pod's own refusals come back in the lane's 126 shape;
  7. the classification mirrors the box; the migration, the op module target,
     the build contract, the registry and the skill text.

Run: APP_ROOT=<repo> python3 scripts/test_host_ops_rung6.py
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "scripts"))

failures: list[str] = []
asyncio.set_event_loop(asyncio.new_event_loop())


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def src(rel: str) -> str:
    return (ROOT / rel).read_text()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


from server.network import nac_approval, nac_ops  # noqa: E402
from server.routes import tickets as tk  # noqa: E402

# ── 1. the digest and the bundle ─────────────────────────────────────────────
print("digest and bundle")
check("fixed vector matches the Go test (ops_test.go TestOpDigestIsTheFramedSHA256)",
      nac_ops.op_dir_digest([("run", 0o755, b"b"), ("op.yaml", 0o644, b"a")])
      == "3392b4eb3a6c228079e011fed9e829b250d93a5ce2ff1ed5b4a3f1e8a0cd774d")
check("one changed byte is a different op",
      nac_ops.op_dir_digest([("run", 0o755, b"c"), ("op.yaml", 0o644, b"a")])
      != nac_ops.op_dir_digest([("run", 0o755, b"b"), ("op.yaml", 0o644, b"a")]))
check("the two modes the box loads", nac_ops.file_mode("run") == 0o755 and nac_ops.file_mode("lib/x.py") == 0o644)

MANIFEST = """name: {name}
version: 1.0.0
summary: Write hello.txt under the test folder.
target: machine
privilege: {priv}
interpreter: /bin/sh
time_limit: 30
params:
  name: {{type: string, max_len: 32}}
  count: {{type: integer, min: 1, max: 5}}
effects:
  files: ["/srv/hostop/"]
modes: [dry_run, apply, verify]
undo: none
"""

projects = Path(tempfile.mkdtemp(prefix="rung6-projects-"))


def make_op(project: int, module: str, name: str, priv: str = "root") -> Path:
    d = projects / str(project) / module / "ops" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "op.yaml").write_text(MANIFEST.format(name=name, priv=priv))
    (d / "run").write_text("#!/bin/sh\necho hi\n")
    (d / "test").mkdir(exist_ok=True)
    (d / "test" / "fixture.txt").write_text("never shipped\n")
    (d / ".hidden").write_text("x")
    return d


d_root = make_op(1, "ops-mod", "write-hello", "root")
d_user = make_op(1, "ops-mod", "write-user", "user")
files = nac_ops.read_op_dir(d_root)
check("the bundle is op.yaml and run, without test/ or dot-entries",
      sorted(f[0] for f in files) == ["op.yaml", "run"] and dict((f[0], f[1]) for f in files) == {"op.yaml": 0o644, "run": 0o755})
wire = json.loads(base64.b64decode(nac_ops.bundle_b64(files)))
check("the wire form is the box's file list", wire[0]["path"] == "op.yaml" and wire[1]["mode"] == 0o755
      and base64.b64decode(wire[1]["b64"]).startswith(b"#!/bin/sh"))
(d_root / "link").symlink_to("/etc/passwd")
try:
    nac_ops.read_op_dir(d_root)
    check("a link inside the op is refused", False)
except nac_ops.OpError as e:
    check("a link inside the op is refused", "link" in e.detail)
(d_root / "link").unlink()
m = nac_ops.parse_manifest(files)
check("the manifest's fields", m["name"] == "write-hello" and m["version"] == "1.0.0" and m["privilege"] == "root"
      and m["effects"] == ["/srv/hostop/"] and set(m["params"]) == {"name", "count"})
for label, bad in {
    "no effects": MANIFEST.replace('files: ["/srv/hostop/"]', "files: []"),
    "privilege neither": MANIFEST.replace("privilege: {priv}", "privilege: admin"),
    "version shape": MANIFEST.replace("version: 1.0.0", "version: v1"),
    # the box's parseOpManifest, mirrored: each of these is a strike if it reached op_sync
    "a two-line summary": MANIFEST.replace("summary: Write hello.txt under the test folder.", 'summary: "one\\ntwo"'),
    "a host target": MANIFEST.replace("target: machine", "target: host"),
    "an interpreter of its own": MANIFEST.replace("interpreter: /bin/sh", "interpreter: perl"),
    "time_limit past the box": MANIFEST.replace("time_limit: 30", "time_limit: 900"),
    "no verify mode": MANIFEST.replace("modes: [dry_run, apply, verify]", "modes: [dry_run, apply]"),
    "undo unstated": MANIFEST.replace("undo: none", "undo: maybe"),
    "undo run without the mode": MANIFEST.replace("undo: none", "undo: run"),
    "an integer with no max": MANIFEST.replace("count: {{type: integer, min: 1, max: 5}}", "count: {{type: integer, min: 1}}"),
    "a string with no max_len": MANIFEST.replace("name: {{type: string, max_len: 32}}", "name: {{type: string}}"),
    "a path rooted at /": MANIFEST.replace("count: {{type: integer, min: 1, max: 5}}", "count: {{type: path, root: /}}"),
    "an effect under /tmp": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/tmp/x/"]'),
    "the root as an effect": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/"]'),
    "a placeholder in an effect": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/srv/{{name}}/"]'),
    "a unit effect": MANIFEST.replace("effects:\n", "effects:\n  units: [nginx]\n"),
    # the box's decoder is typed: these read differently in PyYAML and must be refused here
    "time_limit quoted": MANIFEST.replace("time_limit: 30", 'time_limit: "30"'),
    "time_limit true": MANIFEST.replace("time_limit: 30", "time_limit: true"),
    "a summary over 200 BYTES": MANIFEST.replace("summary: Write hello.txt under the test folder.", "summary: " + "—" * 70),
    "a summary that is a list": MANIFEST.replace("summary: Write hello.txt under the test folder.", "summary: [a, b]"),
    "modes as a string": MANIFEST.replace("modes: [dry_run, apply, verify]", "modes: dry_run apply verify"),
    "a duplicate key": MANIFEST.replace("undo: none", "undo: none\nundo: none"),
    "an unclosed glob": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/srv/hostop/[abc"]'),
    "units as a string": MANIFEST.replace("effects:\n", "effects:\n  units: ''\n"),
    "an integer past int64": MANIFEST.replace("count: {{type: integer, min: 1, max: 5}}", "count: {{type: integer, min: 1, max: 99999999999999999999}}"),
    "an enum with a non-string": MANIFEST.replace("name: {{type: string, max_len: 32}}", "name: {{type: enum, values: [1, 2]}}"),
    "a sexagesimal time_limit": MANIFEST.replace("time_limit: 30", "time_limit: 1:30"),
    "a class starting with ]": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/srv/hostop/[]a]"]'),
    "a negated class starting with ]": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/srv/hostop/[^]]x"]'),
    "a range with no high": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/srv/hostop/[a-]"]'),
    "a range with no low": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/srv/hostop/[-a]"]'),
    "a backslash": MANIFEST.replace('files: ["/srv/hostop/"]', 'files: ["/srv/hostop/a\\\\b"]'),
}.items():
    try:
        nac_ops.parse_manifest([("op.yaml", 0o644, bad.format(name="x", priv="root").encode()), ("run", 0o755, b"")])
        check(f"manifest refused: {label}", False)
    except nac_ops.OpError:
        check(f"manifest refused: {label}", True)
m_yes = nac_ops.parse_manifest([("op.yaml", 0o644, MANIFEST.format(name="x", priv="root").replace(
    "name: {type: string, max_len: 32}", "name: {type: enum, values: [on, off, yes, no]}").encode()), ("run", 0o755, b"")])
check("yes / no / on / off are enum STRINGS, as the box reads them", m_yes["params"]["name"]["values"] == ["on", "off", "yes", "no"])
m_200 = nac_ops.parse_manifest([("op.yaml", 0o644, MANIFEST.format(name="x", priv="root").replace(
    "summary: Write hello.txt under the test folder.", "summary: " + "a" * 200).encode()), ("run", 0o755, b"")])
check("a 200-byte summary is the box's limit exactly", len(m_200["summary"]) == 200)

# ── 2. the purposes ───────────────────────────────────────────────────────────
print("purposes")
import hashlib  # noqa: E402
sha = "a" * 64
ch = "b" * 64
check("op_run purpose is ops.go purposeForOpRun",
      nac_ops.purpose_run("box-7", "write-hello", sha, '{"count":1}', "2026-09-25T00:00:00Z", ch)
      == f"op_run|box-7|write-hello|{sha}|{hashlib.sha256(b'{\"count\":1}').hexdigest()}|2026-09-25T00:00:00Z|{ch}")
check("op_grant purpose is ops.go purposeForOpGrant",
      nac_ops.purpose_grant("box-7", "write-hello", sha, "{}", "2026-10-24T00:00:00Z", 3)
      == f"op_grant|box-7|write-hello|{sha}|{hashlib.sha256(b'{}').hexdigest()}|2026-10-24T00:00:00Z|3")
check("params are canonical: sorted, compact", nac_ops.canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}')

# ── stubs ────────────────────────────────────────────────────────────────────
from server.routes import project_modules  # noqa: E402
project_modules.PROJECTS_ROOT = projects

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from server.network.pairing_grants import canonical  # noqa: E402

KEY = Ed25519PrivateKey.generate()


async def _sign(pool, eid, terms):
    raw = canonical(terms)
    return base64.b64encode(raw).decode(), base64.b64encode(KEY.sign(raw)).decode()

nac_ops._sign = _sign


def terms_of(b64: str) -> dict:
    return json.loads(base64.b64decode(b64))


def verify(terms_b64: str, sig_b64: str) -> bool:
    try:
        KEY.public_key().verify(base64.b64decode(sig_b64), base64.b64decode(terms_b64))
        return True
    except Exception:
        return False


# The stubs are shared with test_host_ops_rung7.py (scripts/host_ops_stubs.py).
from host_ops_stubs import MRelay, Pool, Relay  # noqa: E402


def card_once(name: str, params: str) -> int:
    """Rung 7: an apply first puts the card in front of the person; the
    person's 'Do it once' is what files the device ask. Returns the ticket."""
    res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", [name, params], wait_s=0))
    tid = json.loads(res["stdout"])["ticket_id"]
    run(tk.decide_ticket_service(pool, 9003, tid, option_id="once", actor="user"))
    return tid


from server.network import nac_dcn, nac_mgmt  # noqa: E402

relay = Relay()
nac_dcn._dispatch_box = relay
nac_approval.POLL_S = 0.01
mrelay = MRelay()
nac_mgmt.get_mgmt_relay = lambda: mrelay
audits: list = []


async def _audit(pool, eid, server, op, exit_code=None, decision=None, **kw):
    audits.append((server, op, exit_code, decision))

nac_mgmt.audit_mgmt = _audit

GATING = {"write_gate": True, "device": True, "second_factor": True, "device_keys": ["K1"], "op_view_closed": True}
pool = Pool(enforcing=GATING, devices={"K1": (55, "phone")})
PARAMS = '{"count":2,"name":"x"}'

# ── 3. op_dry_run ─────────────────────────────────────────────────────────────
print("op_dry_run")
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "x", "count": 2}']))
ops_called = [c[1] for c in mrelay.calls]
check("the op is synced then dry-run", ops_called == ["op_sync", "op_dry_run"])
sync_args = mrelay.calls[0][2]
t = terms_of(sync_args[0])
check("op_sync carries terms, signature and the bundle", len(sync_args) == 3 and verify(sync_args[0], sync_args[1])
      and json.loads(base64.b64decode(sync_args[2]))[0]["path"] == "op.yaml")
check("op_run terms: one use, a nonce, canonical params, this machine, the op's sha256",
      t["kind"] == "op_run" and t["uses"] == 1 and len(t["nonce"]) == 32 and t["params"] == PARAMS
      and t["server"] == "box-7" and t["sha256"] == nac_ops.op_dir_digest(nac_ops.read_op_dir(d_root))
      and t["privilege"] == "root" and t["entity"] == 9003 and "approval" not in t and "changes_sha256" not in t)
exp = dt.datetime.strptime(t["expires"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
left = (exp - dt.datetime.now(dt.timezone.utc)).total_seconds()
check("the terms expire within the box's 24 h (4 h here)", 3.9 * 3600 < left <= 4 * 3600)
check("op_dry_run takes the same terms, no bundle", mrelay.calls[1][2] == sync_args[:2])
out = json.loads(res["stdout"])
check("the result is the change list and what to do next, nothing landed",
      res["exit"] == 0 and out["state"] == "dry_run" and "created /srv/hostop/hello.txt" in out["changes"]
      and "+ hello" in out["changes"] and out["changes_sha256"] == "c" * 64 and "op_apply" in out["next"])
check("the dry run is kept with its change list, its expiry a datetime for the database",
      pool.runs and pool.runs[-1]["state"] == "dry_run" and pool.runs[-1]["changes_sha256"] == "c" * 64
      and pool.runs[-1]["nonce"] == t["nonce"] and isinstance(pool.runs[-1]["expires"], dt.datetime))
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "x"}']))
    check("a missing param is refused here, never sent to the box (a refusal there is a strike)", False)
except nac_ops.OpError as e:
    check("a missing param is refused here, never sent to the box (a refusal there is a strike)",
          "missing" in e.detail and mrelay.calls[-1][1] != "op_sync" or "count" in e.detail)
for label, bad in {"an integer out of range": '{"name": "x", "count": 9}', "a string too long": '{"name": "' + "y" * 40 + '", "count": 1}',
                   "a bool for an int": '{"name": "x", "count": true}', "NaN": '{"name": "x", "count": NaN}',
                   "Infinity": '{"name": "x", "count": Infinity}', "1e400": '{"name": "x", "count": 1e400}',
                   "past int64": '{"name": "x", "count": 99999999999999999999}'}.items():
    n = len(mrelay.calls)
    try:
        run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", bad]))
        check(f"{label}: refused here", False)
    except nac_ops.OpError:
        check(f"{label}: refused here, nothing sent", len(mrelay.calls) == n)
check("audited", ("box-7", "op_dry_run", 0, "dry-run") in audits)
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"nope": 1}']))
    check("a param op.yaml does not declare is refused before the box", False)
except nac_ops.OpError as e:
    check("a param op.yaml does not declare is refused before the box", "nope" in e.detail and e.status == 400)
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["no-such-op", "{}"]))
    check("an op nobody built is named as missing", False)
except nac_ops.OpError as e:
    check("an op nobody built is named as missing", e.status == 404 and "target op" in e.detail)
mrelay.refuse["op_dry_run"] = "op_dry_run refused: the run changed /etc/x, which is outside the op's declared effects"
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "y", "count": 2}']))
check("the box's refusal is the answer, in the lane's 126 shape",
      res["exit"] == 126 and "outside the op's declared effects" in res["stderr"] and pool.runs[-1]["state"] == "refused")
del mrelay.refuse["op_dry_run"]

# ── 4. op_apply ───────────────────────────────────────────────────────────────
print("op_apply")
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "z", "count": 2}']))
    check("an apply with no live dry run is refused with the way on", False)
except nac_ops.OpError as e:
    check("an apply with no live dry run is refused with the way on", e.status == 409 and "op_dry_run" in e.detail)
mrelay.calls.clear()
relay.calls.clear()
relay.device_state = "pending"
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"count": 2, "name": "x"}'], wait_s=0))
check("the card first (rung 7): 'asked', nothing on the device, nothing on the box",
      res["exit"] == 0 and json.loads(res["stdout"])["state"] == "asked" and not relay.calls and not mrelay.calls)
run(tk.decide_ticket_service(pool, 9003, json.loads(res["stdout"])["ticket_id"], option_id="once", actor="user"))
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"count": 2, "name": "x"}'], wait_s=0))
check("pending: a pending result, nothing dispatched to the box, the run awaiting",
      res.get("state") == "pending_approval" and res["exit"] is None and not mrelay.calls
      and pool.runs[0]["state"] == "awaiting" and "device" in res["next"])
filed = [c for c in relay.calls if c[1] == "device_request"]
details = json.loads(base64.b64decode(filed[0][2][0]))
dry = pool.runs[0]
dry_expires = dry["expires"].strftime("%Y-%m-%dT%H:%M:%SZ")
check("the device is asked with the op_run details — the terms' fields and the change list",
      details["kind"] == "op_run" and details["sha256"] == dry["sha256"] and details["params"] == PARAMS
      and details["expires"] == dry_expires and details["changes_sha256"] == "c" * 64 and details["uses"] == 1
      and details["server"] == "box-7" and details["privilege"] == "root" and "+ hello" in details["dry_run"]
      and details["effects"] == ["/srv/hostop/"])
check("the purpose the device derives from those details is the box's",
      res["request"]["purpose"] == nac_ops.purpose_run("box-7", "write-hello", dry["sha256"], PARAMS, dry_expires, "c" * 64))
relay.device_state = "approved"
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"count": 2, "name": "x"}'], wait_s=1))
ops_called = [c[1] for c in mrelay.calls]
check("approved: synced (idempotent) then applied", ops_called == ["op_sync", "op_apply"])
ta = terms_of(mrelay.calls[1][2][0])
check("the apply's terms carry the approval, the dry run's nonce and its change list, and verify",
      ta["approval"] == '{"v":1,"sig":"x"}' and ta["nonce"] == dry["nonce"] and ta["changes_sha256"] == "c" * 64
      and ta["expires"] == dry_expires and ta["params"] == PARAMS and verify(mrelay.calls[1][2][0], mrelay.calls[1][2][1]))
out = json.loads(res["stdout"])
check("applied: committed list and the run marked applied with its approval",
      res["exit"] == 0 and out["state"] == "applied" and out["committed"] == ["created /srv/hostop/hello.txt"]
      and pool.runs[0]["state"] == "applied" and pool.runs[0]["approval"] == '{"v":1,"sig":"x"}'
      and audits[-1][3] == "applied")
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", PARAMS]))
    check("an applied run is not applied twice from the pod either", False)
except nac_ops.OpError as e:
    check("an applied run is not applied twice from the pod either", e.status == 409)
# A box refusal on apply: the box's words, the way on, the run refused.
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "q", "count": 1}']))
card_once("write-hello", '{"name": "q", "count": 1}')
mrelay.refuse["op_apply"] = "op_apply refused: refused before commit: the apply would change something other than what the dry run showed"
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "q", "count": 1}'], wait_s=1))
check("a box refusal on apply: its words, plus dry-run again; the run refused",
      res["exit"] == 126 and "other than what the dry run showed" in res["stderr"] and "dry-run again" in res["stderr"]
      and pool.runs[-1]["state"] == "refused")
del mrelay.refuse["op_apply"]
# The apply's answer lost: the row is 'sent', a retry is refused with the truth, and the
# box's own "applied already" closes it as applied.
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "t", "count": 1}']))
card_once("write-hello", '{"name": "t", "count": 1}')
from server.network import nac_mgmt as _nm  # noqa: E402
_orig = mrelay.dispatch_detailed


async def _lost(token_id, op, args, timeout, approval=""):
    if op == "op_apply":
        mrelay.calls.append((token_id, op, list(args), timeout))
        return None, _nm.DISPATCH_NO_REPLY
    return await _orig(token_id, op, args, timeout, approval)

mrelay.dispatch_detailed = _lost
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t", "count": 1}'], wait_s=1))
    check("a lost answer is a 504, never a refusal", False)
except nac_ops.OpError as e:
    check("a lost answer is a 504, never a refusal", e.status == 504 and pool.runs[-1]["state"] == "sent")
mrelay.dispatch_detailed = _orig
sent_row = pool.runs[-1]
# (a) the box says the run was attempted and REFUSED (the nonce burns before the op runs): refused, never applied
relay.calls.clear()
mrelay.calls.clear()
mrelay.status = {"found": True, "nonce_burned": True, "applied": False, "error": "refused before commit: the apply would change something other than what the dry run showed"}
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t", "count": 1}'], wait_s=1))
check("a lost answer is read from the box: attempted and refused there reads as refused, never applied",
      not relay.calls and [c[1] for c in mrelay.calls] == ["op_run_status"] and res["exit"] == 126
      and "other than what the dry run showed" in res["stderr"] and sent_row["state"] == "refused")
# a commit that stopped part-way names what landed — never a plain refusal
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "p", "count": 1}']))
card_once("write-hello", '{"name": "p", "count": 1}')
mrelay.dispatch_detailed = _lost
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "p", "count": 1}'], wait_s=1))
except nac_ops.OpError:
    pass
mrelay.dispatch_detailed = _orig
mrelay.status = {"found": True, "nonce_burned": True, "applied": False, "error": "commit: disk full (1 change(s) landed before it)",
                 "committed": ["created_dir /srv/hostop", "created /srv/hostop/hello.txt"]}
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "p", "count": 1}'], wait_s=1))
check("a commit that stopped part-way: what landed is named, never a plain refusal",
      res["exit"] == 126 and "LANDED" in res["stderr"] and "/srv/hostop/hello.txt" in res["stderr"] and pool.runs[-1]["state"] == "refused"
      and pool.runs[-1]["result"].get("partial") is True)
# applied with a failed verify: applied, and the verify said
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "v", "count": 1}']))
card_once("write-hello", '{"name": "v", "count": 1}')
mrelay.dispatch_detailed = _lost
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "v", "count": 1}'], wait_s=1))
except nac_ops.OpError:
    pass
mrelay.dispatch_detailed = _orig
mrelay.status = {"found": True, "nonce_burned": True, "applied": True, "error": "verify: the sandbox could not start",
                 "committed": ["created /srv/hostop/hello.txt"], "verify_error": "the sandbox could not start"}
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "v", "count": 1}'], wait_s=1))
check("applied with a failed verify reads as applied, the verify said",
      res["exit"] == 0 and json.loads(res["stdout"])["state"] == "applied" and "verify" in json.loads(res["stdout"])["note"]
      and pool.runs[-1]["state"] == "applied")
# (b) attempted, record gone: the outcome is not knowable — said as it is, the row stays 'sent'
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "t2", "count": 1}']))
card_once("write-hello", '{"name": "t2", "count": 1}')
mrelay.dispatch_detailed = _lost
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t2", "count": 1}'], wait_s=1))
except nac_ops.OpError:
    pass
mrelay.dispatch_detailed = _orig
sent2 = pool.runs[-1]
mrelay.status = {"found": False, "nonce_burned": True}
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t2", "count": 1}'], wait_s=1))
    check("attempted with the record gone: not knowable, said so", False)
except nac_ops.OpError as e:
    check("attempted with the record gone: not knowable, said so", e.status == 409 and "cannot be read" in e.detail and sent2["state"] == "sent")
# (c) the job never arrived (nonce unburned): the SAME run goes again under its approval, no fresh ask
mrelay.status = {"found": False, "nonce_burned": False}
relay.calls.clear()
mrelay.calls.clear()
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t2", "count": 1}'], wait_s=1))
ta2 = terms_of(mrelay.calls[-1][2][0])
check("never arrived: the same run is re-sent under its approval, no fresh ask, and lands",
      not relay.calls and [c[1] for c in mrelay.calls] == ["op_run_status", "op_sync", "op_apply"]
      and ta2["nonce"] == sent2["nonce"] and ta2["approval"] == sent2["approval"] and res["exit"] == 0
      and sent2["state"] == "applied")
# (d) a lapsed approval is never re-sent
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "t3", "count": 1}']))
card_once("write-hello", '{"name": "t3", "count": 1}')
mrelay.dispatch_detailed = _lost
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t3", "count": 1}'], wait_s=1))
except nac_ops.OpError:
    pass
mrelay.dispatch_detailed = _orig
sent3 = pool.runs[-1]
sent3["approval"] = json.dumps({"v": 1, "sig": "x", "expires": "2020-01-01T00:00:00Z"})
mrelay.calls.clear()
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t3", "count": 1}'], wait_s=1))
check("never arrived but the approval lapsed: refused with the way on, nothing re-sent",
      res["exit"] == 126 and "lapsed" in res["stderr"] and [c[1] for c in mrelay.calls] == ["op_run_status"]
      and sent3["state"] == "refused")
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "t5", "count": 1}']))
card_once("write-hello", '{"name": "t5", "count": 1}')
mrelay.dispatch_detailed = _lost
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t5", "count": 1}'], wait_s=1))
except nac_ops.OpError:
    pass
mrelay.dispatch_detailed = _orig
sent5 = pool.runs[-1]
sent5["approval"] = "console-id-1"
sent5["created_at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
mrelay.calls.clear()
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t5", "count": 1}'], wait_s=1))
check("a console approval lapses with the box's day: nothing re-sent after it",
      res["exit"] == 126 and "lapsed" in res["stderr"] and [c[1] for c in mrelay.calls] == ["op_run_status"])
# (e) a box that cannot answer op_run_status (older than 0.37.0): said as it is
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "t4", "count": 1}']))
card_once("write-hello", '{"name": "t4", "count": 1}')
mrelay.dispatch_detailed = _lost
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t4", "count": 1}'], wait_s=1))
except nac_ops.OpError:
    pass
mrelay.dispatch_detailed = _orig
mrelay.refuse["op_run_status"] = "unknown op"
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "t4", "count": 1}'], wait_s=1))
    check("a box that cannot report the run: said as it is, the row stays sent", False)
except nac_ops.OpError as e:
    check("a box that cannot report the run: said as it is, the row stays sent", e.status == 409 and "cannot report" in e.detail
          and pool.runs[-1]["state"] == "sent")
del mrelay.refuse["op_run_status"]
mrelay.status = {"found": False, "nonce_burned": False}
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "u", "count": 1}']))
card_once("write-hello", '{"name": "u", "count": 1}')
mrelay.refuse["op_apply"] = "op_apply refused: this op_run was applied already"
mrelay.status = {"found": True, "nonce_burned": True, "applied": True, "committed": ["created /srv/hostop/hello.txt"], "changes_sha256": "c" * 64}
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "u", "count": 1}'], wait_s=1))
check("the box's 'applied already' is resolved from op_run_status: applied there reads as applied, with its note",
      res["exit"] == 0 and json.loads(res["stdout"])["state"] == "applied" and "lost" in json.loads(res["stdout"])["note"]
      and json.loads(res["stdout"])["committed"] == ["created /srv/hostop/hello.txt"] and pool.runs[-1]["state"] == "applied")
mrelay.status = {"found": False, "nonce_burned": True}
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "u2", "count": 1}']))
card_once("write-hello", '{"name": "u2", "count": 1}')
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "u2", "count": 1}'], wait_s=1))
    check("'applied already' with no record: never called applied", False)
except nac_ops.OpError as e:
    check("'applied already' with no record: never called applied", e.status == 409 and pool.runs[-1]["state"] == "sent")
del mrelay.refuse["op_apply"]
mrelay.status = {"found": False, "nonce_burned": False}
# Denied on the device.
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-hello", '{"name": "r", "count": 1}']))
card_once("write-hello", '{"name": "r", "count": 1}')
relay.device_state = "denied"
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-hello", '{"name": "r", "count": 1}'], wait_s=1))
check("denied on the device: 126, denied-person, nothing dispatched",
      res["exit"] == 126 and "refused" in res["stderr"] and audits[-1][3] == "denied-person"
      and mrelay.calls[-1][1] != "op_apply")
relay.device_state = "approved"
# A box that gates nothing cannot take an op apply either: said, not sent.
ungated = Pool(enforcing={"grants": [], "op_view_closed": True})
run(nac_ops.run_door(ungated, 9003, "box-7", 77, "op_dry_run", ["write-hello", PARAMS]))
mrelay.calls.clear()
try:
    run(nac_ops.run_door(ungated, 9003, "box-7", 77, "op_apply", ["write-hello", PARAMS], wait_s=1))
    check("a box reporting no write gate: refused with the reason before any card, never sent unapproved terms", False)
except nac_ops.OpError as e:
    check("a box reporting no write gate: refused with the reason before any card, never sent unapproved terms",
          e.status == 409 and "write gate" in e.detail and not mrelay.calls and not ungated.tickets)

# ── 5. op_grant ───────────────────────────────────────────────────────────────
print("op_grant")
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_grant", ["write-hello", '{"count": {"min": 1, "max": 2}}']))
    check("a grant for a root op is refused: root is never reusable", False)
except nac_ops.OpError as e:
    check("a grant for a root op is refused: root is never reusable", "never reusable" in e.detail)
mrelay.calls.clear()
relay.calls.clear()
relay.device_state = "pending"
BOUNDS = '{"count":{"max":2,"min":1}}'
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_grant", ["write-user", '{"count": {"min": 1, "max": 2}}', "5", "10"], wait_s=0))
    check("a grant is never asked for without a dry run inside its bounds (rung 7: a card never appears without one)", False)
except nac_ops.OpError as e:
    check("a grant is never asked for without a dry run inside its bounds (rung 7: a card never appears without one)",
          e.status == 409 and "op_dry_run" in e.detail)
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-user", '{"name": "x", "count": 2}']))
mrelay.calls.clear()
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_grant", ["write-user", '{"count": {"min": 1, "max": 2}}', "5", "10"], wait_s=0))
gt = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
check("op_grant raises the card on that dry run — from now on recommended, the bounds, uses and days proposed — nothing synced",
      json.loads(res["stdout"])["state"] == "asked" and gt["metadata"]["op_ask"]["grant"]["bounds"] == BOUNDS
      and gt["metadata"]["op_ask"]["grant"]["uses"] == 5 and gt["options"][1]["id"] == "grant"
      and gt["options"][1]["recommend"] is True and not mrelay.calls and not relay.calls)
for label, bad in {"a field the type does not read": '{"count": {"values": ["1"]}}', "wider than the op": '{"count": {"min": 0, "max": 9}}',
                   "an unknown field": '{"count": {"min": 1, "root": "/x"}}', "a param the op lacks": '{"nope": {"min": 1}}'}.items():
    try:
        run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_grant", ["write-user", bad, "5", "10"], wait_s=0))
        check(f"bounds refused here: {label}", False)
    except nac_ops.OpError:
        check(f"bounds refused here: {label}", True)
n_before = len(pool.tickets)
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_grant", ["write-user", '{"count": {"min": 1, "max": 2}}', "5", "10"], wait_s=0))
check("asked again while the card is open: the same card, no second ticket",
      len(pool.tickets) == n_before and json.loads(res["stdout"])["ticket_id"] == gt["id"])
run(tk.decide_ticket_service(pool, 9003, gt["id"], option_id="grant", actor="user"))
g = pool.runs[-1]
check("the person's 'from now on' makes the grant row under one nonce, awaiting, and asks the device; nothing synced",
      g["kind"] == "op_grant" and g["state"] == "awaiting" and g["bounds"] == BOUNDS and g["uses"] == 5 and not mrelay.calls)
details = json.loads(base64.b64decode([c for c in relay.calls if c[1] == "device_request"][0][2][0]))
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_grant", ["write-user", '{"count": {"min": 1, "max": 2}}', "5", "10"], wait_s=0))
check("the device is asked with the op_grant details, and the Host's next call waits on the same ask",
      res.get("state") == "pending_approval"
      and details["kind"] == "op_grant" and details["bounds"] == BOUNDS and details["uses"] == 5
      and details["privilege"] == "user" and details["expires"] == g["expires"].strftime("%Y-%m-%dT%H:%M:%SZ")
      and res["request"]["purpose"] == nac_ops.purpose_grant("box-7", "write-user", g["sha256"], BOUNDS, details["expires"], 5))
exp = g["expires"]
check("expires in the days asked (≤ 30)", 9.9 * 86400 < (exp - dt.datetime.now(dt.timezone.utc)).total_seconds() <= 10 * 86400)
relay.device_state = "approved"
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_grant", ["write-user", '{"count": {"min": 1, "max": 2}}', "5", "10"], wait_s=1))
tg = terms_of(mrelay.calls[0][2][0])
check("approved: op_sync with the grant terms — user, bounds, uses, the approval — installed, and the card's own run goes through it",
      [c[1] for c in mrelay.calls] == ["op_sync", "op_grant_run"] and len(mrelay.calls[0][2]) == 3 and tg["kind"] == "op_grant"
      and tg["privilege"] == "user" and tg["bounds"] == BOUNDS and tg["uses"] == 5 and tg["nonce"] == g["nonce"]
      and tg["approval"] == '{"v":1,"sig":"x"}' and g["state"] == "installed" and res["exit"] == 0
      and json.loads(res["stdout"])["state"] == "applied" and json.loads(res["stdout"])["grant"]["uses"] == 5
      and ("box-7", "op_grant", 0, "grant-installed") in audits)
mrelay.calls.clear()
relay.calls.clear()
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-user", '{"name": "x", "count": 2}'], wait_s=0))
check("under the grant an apply runs through op_grant_run with no tap, and is recorded",
      res["exit"] == 0 and mrelay.calls[-1][1] == "op_grant_run" and mrelay.calls[-1][2] == [g["nonce"], "apply", '{"count":2,"name":"x"}']
      and not relay.calls and json.loads(res["stdout"])["state"] == "applied" and audits[-1][3] == "applied-grant"
      and pool.runs[-1]["state"] == "applied" and json.loads(pool.runs[-1]["report"])["under_grant"] == g["nonce"])
# Params outside the grant's bounds take the once-path — a dry run of their own, never a
# refusal from the box (which would count against the host).
mrelay.calls.clear()
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-user", '{"name": "x", "count": 5}']))
check("params outside the grant's bounds take the once-path", [c[1] for c in mrelay.calls] == ["op_sync", "op_dry_run"])
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-user", '{"name": "x", "count": 2}']))
check("and so does a dry run", mrelay.calls[-1][1] == "op_grant_run" and mrelay.calls[-1][2][1] == "dry_run")
# A dry run under the grant never feeds a once-path apply (its nonce and expiry are the grant's).
grant_dry = pool.runs[-1]
check("the grant's dry run is recorded as such", json.loads(grant_dry["report"])["under_grant"] == g["nonce"]
      and grant_dry["state"] == "dry_run")
# The lost answer of a grant apply leaves its row 'sent' and counts as a use here.
mrelay.dispatch_detailed = _lost.__class__  # placeholder, replaced below


async def _lost_grant(token_id, op, args, timeout, approval=""):
    if op == "op_grant_run":
        mrelay.calls.append((token_id, op, list(args), timeout))
        return None, _nm.DISPATCH_NO_REPLY
    return await _orig(token_id, op, args, timeout, approval)

mrelay.dispatch_detailed = _lost_grant
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-user", '{"name": "x", "count": 2}'], wait_s=0))
    check("a lost grant-run answer is a 504 with the row left 'sent'", False)
except nac_ops.OpError as e:
    check("a lost grant-run answer is a 504 with the row left 'sent'", e.status == 504 and pool.runs[-1]["state"] == "sent"
          and "second apply" in e.detail)
mrelay.dispatch_detailed = _orig
# The same params are not sent again inside the run's own budget — the
# machine may still be running the lost one. Past it (aged here) the run goes and says so.
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-user", '{"name": "x", "count": 2}'], wait_s=0))
    check("the same params again, inside the run's budget, are held back with the words", False)
except nac_ops.OpError as e:
    check("the same params again, inside the run's budget, are held back with the words", e.status == 409 and "answer was lost" in e.detail)
pool.runs[-1]["created_at"] -= dt.timedelta(seconds=nac_mgmt.mgmt_op_budget("op_grant_run") + 5)
# No worker: nothing was dispatched, so nothing is counted and the row is not 'sent'.
_wc = mrelay.worker_count
mrelay.worker_count = lambda token_id: 0
try:
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-user", '{"name": "x", "count": 2}'], wait_s=0))
    check("no worker under a grant: 502, the row refused, no use counted", False)
except nac_ops.OpError as e:
    check("no worker under a grant: 502, the row refused, no use counted", e.status == 502 and pool.runs[-1]["state"] == "refused"
          and json.loads(pool.runs[-1]["report"])["spent"] is False)
mrelay.worker_count = _wc
# A refusal AFTER the box took the use (the run itself failed) counts as one here too.
mrelay.refuse["op_grant_run"] = "op_grant_run refused: refused: the run changed /etc/x, which is outside the op's declared effects"
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-user", '{"name": "x", "count": 2}'], wait_s=0))
check("a refusal after the spend counts as a use here", json.loads(pool.runs[-1]["report"])["spent"] is True)
del mrelay.refuse["op_grant_run"]
for words, want in {"op_grant_run refused: the op exited 2 in apply mode: param x approved": True,
                    "op_grant_run refused: op grant has no uses left": False,
                    "refused: this host's write lanes are quarantined on this machine until later": False,
                    "op_grant_run refused: param \"count\" is outside the approved range": False,
                    "op_grant_run refused: commit: disk full": True,
                    "op_grant_run refused: refused: this machine's own record cannot be located; ops are refused": True,
                    "op_grant_run refused: cannot read the machine's mounts: boom": True,
                    "op_grant_run refused: effect /nfs/x is on a nfs4 mount at /nfs, which the sandbox does not carry": True,
                    "op_grant_run refused: the sandbox could not run the op: fork/exec: permission denied": True,
                    # gate refusals arrive bare through refuseMgmt, before any handler: never a use
                    "refused: 'ops' is switched off on this machine by the person at it": False,
                    "refused: op 'op_grant_run' is family 'ops', not in this pairing's caps": False,
                    "refused: op 'op_grant_run' — this machine is networked but NOT ANCHORED": False,
                    "refused: op 'op_grant_run' not permitted on this host": False,
                    "refused: this pairing holds no verified pairing grant": False,
                    "something new the box says": False}.items():
    check(f"spent by the box's words, anchored: {words[:40]}", nac_ops.grant_use_spent(words) is want)
mrelay.refuse["op_grant_run"] = "refused: this host's write lanes are quarantined on this machine until 2099-01-01T00:00:00Z"
run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-user", '{"name": "x", "count": 2}'], wait_s=0))
check("a quarantine refusal is not a spent use", json.loads(pool.runs[-1]["report"])["spent"] is False)
del mrelay.refuse["op_grant_run"]
# Four applies are known spent now (the card's own, one applied, one sent, one refused after the spend)
# against uses=5: one more, then the pod itself takes the once-path and drops its copy, before the box ever refuses.
for _ in range(1):
    run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-user", '{"name": "x", "count": 2}'], wait_s=0))
mrelay.calls.clear()
relay.calls.clear()
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_dry_run", ["write-user", '{"name": "x", "count": 2}']))
check("every use spent by this side's count: the once-path, the grant dropped here, no refusal from the box",
      [c[1] for c in mrelay.calls] == ["op_sync", "op_dry_run"] and g["state"] == "dropped")
relay.device_state = "pending"
relay.calls.clear()
res = run(nac_ops.run_door(pool, 9003, "box-7", 77, "op_apply", ["write-user", '{"name": "x", "count": 2}'], wait_s=0))
check("and the apply after it asks the person (the card), from the once-path dry run, never from the grant's",
      json.loads(res["stdout"])["state"] == "asked" and not relay.calls
      and pool.tickets[json.loads(res["stdout"])["ticket_id"]]["metadata"]["op_ask"]["expires"]
      == pool.runs[-1]["expires"].strftime("%Y-%m-%dT%H:%M:%SZ"))
relay.device_state = "approved"

# ── 6. the door in run_mgmt_op ────────────────────────────────────────────────
print("run_mgmt_op")


async def _state(pool, eid):
    return [{"server": "*", "ops": ["op_dry_run", "op_apply"]}], set(), set()

nac_mgmt._gate_state = _state
nac_mgmt.mgmt_decision = lambda grants, eid, server, op, suspended, quarantined: op in ("op_dry_run", "op_apply")
nac_mgmt.mgmt_denied_by = lambda grants, server, op: None
mrelay.calls.clear()
res = run(nac_mgmt.run_mgmt_op(pool, 9003, "box-7", "op_grant", ["write-user", "{}"], origin="tool"))
check("ungranted door op: the pod gate's 126, nac_ops never reached",
      res["exit"] == 126 and "not permitted" in res["stderr"] and not mrelay.calls)
res = run(nac_mgmt.run_mgmt_op(pool, 9003, "box-7", "op_dry_run", ["write-hello", '{"name": "m", "count": 1}'], origin="tool"))
check("granted: handed to nac_ops after the gate", res["exit"] == 0 and [c[1] for c in mrelay.calls] == ["op_sync", "op_dry_run"])
res = run(nac_mgmt.run_mgmt_op(pool, 9003, "box-7", "op_apply", ["write-hello", '{"name": "never", "count": 1}'], origin="tool"))
check("the pod's own refusal (no dry run) comes back in the lane's 126 shape, not an error",
      res["exit"] == 126 and "op_dry_run" in res["stderr"])
res = run(nac_mgmt.run_mgmt_op(pool, 9003, "box-7", "op_dry_run", ["write-hello", "[" * 60000], origin="tool"))
check("deeply nested params: refused with the shape named, audited", res["exit"] == 126 and "JSON object" in res["stderr"]
      and audits[-1][3] == "refused-pod")
check("budgets: a dry run waits the sandbox's ceiling, an apply twice it",
      nac_mgmt.mgmt_op_budget("op_dry_run") >= 600 and nac_mgmt.mgmt_op_budget("op_apply") >= 1200)

# ── 7. classification, the migration, the target, the registry, the skill ────
print("wiring")
check("op_list and op_run_status are reads; the four box ops carry their own approval (the terms)",
      not nac_approval.op_writes("op_list") and not nac_approval.op_writes("op_run_status")
      and all(nac_approval.own_approval(o) for o in ("op_sync", "op_dry_run", "op_apply", "op_grant_run")))
mig = src("migrations/v181_host_ops_runs.sql")
check("v181: the module target widens to op and host_op_runs exists",
      "'component','op'" in mig and "CREATE TABLE IF NOT EXISTS host_op_runs" in mig and "changes_sha256" in mig)
from server.applications import target_contracts  # noqa: E402
check("op is a module target on both sides", "op" in project_modules.TARGETS and "op" in target_contracts.TARGETS
      and "op" in project_modules.target_words("op"))
brief = target_contracts.contract_prefix("op", "DEPLOY", "SDK")
check("the build contract for an op module hands the builder the op format, not the app contract",
      "op.yaml" in brief and "effects" in brief and "dry_run" in brief and "DEPLOY" not in brief and "SDK" not in brief)
check("an op module needs no index.html and no app.yaml", target_contracts.checks_for("op") == {"entry": False, "manifest": False})
reg = src("brain/mastercode/public_tool_registry.py")
check("the registry's module target enum carries op",
      '"enum": ["private_app", "public_site", "store_app", "component", "op"' in reg)
from mastercode import public_tool_registry as r  # noqa: E402
desc = {**r.PUBLIC_TOOL_SCHEMAS, **r.OPERATOR_PLATFORM_TOOL_SCHEMAS}["mgmt_run"]["function"]["description"]
check("mgmt_run names the door, under the cap", "op_dry_run" in desc and "op_grant" in desc and len(desc) <= 600)
skill = src("default-skills/network.md")
check("the skill teaches the op lane", "op_dry_run" in skill and "op_grant" in skill and "never reusable" in skill
      and "WRITE AN OP" in skill)
check("this gate is registered (its own file in gates-container-free.d/)",
      (ROOT / "scripts/gates-container-free.d/test_host_ops_rung6.txt").is_file()
      and "test_host_ops_rung6.py" in src("scripts/gates-container-free.d/test_host_ops_rung6.txt"))

print()
if failures:
    print(f"FAIL: {len(failures)} check(s)")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("OK: all checks passed")
