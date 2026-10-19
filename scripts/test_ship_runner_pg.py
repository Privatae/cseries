#!/usr/bin/env python3
"""test_ship_runner_pg.py — the ship runner (server/ships/runner.py) end to
end against a REAL Postgres, with the migration that ships (v194) and a fake
box that signs every receipt with a real machine key.

The fake box keeps the bastion's own line rules (ops_plan.go /
ops_plan_linux.go): the plan must verify under the pinned device key and
name this machine's key; a root line needs a plan approved with the code;
a line runs only after every earlier line's successful receipt came with
it; attempts run one at a time, the next only after one failed, up to the
line's allowance; an undo needs a signed failed receipt of that line or a
later one. The formats themselves are pinned against the bastion's Go by
test_ship_plan.py and test_ship_seal.py.

Scenarios: a clean two-machine ship; a line longer than the dispatch wait
(504) and a dropped connection, both read back with op_plan_status; retry
that passes; undo, newest first; stop; ask (the Host woken under the
pipeline's turn type); denied and lapsed asks; a device answer that is not
this run's plan; one run per pipeline; a machine that refuses; no grant
(refused before any row or ask); Stop between lines; a forged receipt; an
op changed after approval; sealed credentials opened by the machine (where
PyNaCl is installed).

It runs on a fresh clone of the KERNEL's schema (scripts/kernel_schema.py —
entrypoint.sh's procedure, every migration, v194 included) for one Host, made
on the test server for this run and dropped after it. The server is
TEST_DATABASE_URL, PEER_TEST_DSN or TEST_DSN; the gate creates its own database
there and never writes to the one the URL names. Between scenarios it empties
the ship tables and puts its fixture rows back (rows only, never DDL); the
'ships' turn type is drafted and activated once, through
turn_type_approval, the one writer of a type.

It used to DROP and hand-build mcp_client_tokens, network_bastion_heartbeat,
project_modules and turn_types (and the ship tables) on whatever database
TEST_DATABASE_URL named (review of preview-animated at e296d06c:
test_no_hand_kernel_tables refuses both).

SKIPS CLEANLY when no test server is set; with GATE_LANE set that is a
failure. Run:
    PEER_TEST_DSN='postgresql://entity@/postgres?host=<socket folder>&port=5432' APP_ROOT=<repo> python3 scripts/test_ship_runner_pg.py
    (eval "$(scripts/peer_test_db.sh)" starts a throwaway cseries-postgres, reached over the unix socket in
    that folder, and exports its PEER_TEST_DSN in this form)
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

SERVER = (os.environ.get("TEST_DATABASE_URL") or os.environ.get("PEER_TEST_DSN")
          or os.environ.get("TEST_DSN"))
if not SERVER:
    if os.environ.get("GATE_LANE"):
        print(f"FAIL: the {os.environ['GATE_LANE']} lane named no test server (TEST_DATABASE_URL / "
              "PEER_TEST_DSN / TEST_DSN) for this suite")
        sys.exit(1)
    print("SKIPPED: no test server (TEST_DATABASE_URL / PEER_TEST_DSN / TEST_DSN) — this suite needs a "
          "real Postgres it can make its own database on")
    sys.exit(0)

import asyncpg  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from server.network import nac_approval, nac_mgmt, nac_ops  # noqa: E402
from server.routes import project_modules  # noqa: E402
from server.ships import plan as sp  # noqa: E402
from server.ships import runner  # noqa: E402

try:
    from nacl.public import PrivateKey as XPriv, SealedBox
    HAVE_NACL = True
except ImportError:
    HAVE_NACL = False

EID = 3
failures: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def pub_b64(k: Ed25519PrivateKey) -> str:
    return b64(k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def dsse(k: Ed25519PrivateKey, ptype: str, body: bytes) -> dict:
    return {"payloadType": ptype, "payload": b64(body), "signature": b64(k.sign(sp.pae(ptype, body)))}


# ── keys, machines, the device ──────────────────────────────────────────────

DEVICE = Ed25519PrivateKey.generate()
DEVICE_PUB = pub_b64(DEVICE)
HOSTS = {"build-box": 11, "k8s-box": 12}
MKEYS = {h: Ed25519PrivateKey.generate() for h in HOSTS}
XKEYS = {h: XPriv.generate() for h in HOSTS} if HAVE_NACL else {}


def enc_claim(h):
    if not HAVE_NACL:
        return None
    pub = b64(bytes(XKEYS[h].public_key))
    return {"public_key": pub, "machine_key": pub_b64(MKEYS[h]),
            "signature": b64(MKEYS[h].sign(b"encryption-key\n" + pub.encode()))}


class Box:
    """One machine's bastion, as far as plan lines go."""

    def __init__(self, host):
        self.host = host
        self.records = {}     # (nonce, n) -> {"attempt", "state"}
        self.receipts = {}    # (nonce, n) -> [signed]
        self.script = {}      # (n) -> list of outcomes, one per attempt
        self.deferred = {}    # (nonce, n) -> signed, shown by status only
        self.synced = []
        self.refuse_next = None
        self.lines_seen = []
        self.creds_seen = []
        self.forge = False
        self.refusals = []      # every refusal is a strike toward the Host's quarantine (quarantine.go)
        self.status_timeouts = 0
        self.undo_slow = False
        self.undo_no_worker = 0
        self.deferred_undo = {}

    def refuse(self, why):
        self.refusals.append(why)
        return {"exit": 126, "stdout": "", "stderr": why}

    def sign(self, r: dict) -> dict:
        r = {**r, "v": 1, "key": pub_b64(MKEYS[self.host])}
        key = Ed25519PrivateKey.generate() if self.forge else MKEYS[self.host]
        return dsse(key, sp.RECEIPT_PAYLOAD_TYPE, json.dumps(r).encode())

    def open_plan(self, arg):
        env = base64.b64decode(arg).decode()
        return sp.open_envelope(env, device_keys=[DEVICE_PUB])

    def line(self, args):
        if self.refuse_next:
            why, self.refuse_next = self.refuse_next, None
            return self.refuse(f"op_plan_line refused: {why}")
        p, sha = self.open_plan(args[0])
        n = int(args[1])
        ln = p["lines"][n - 1]
        if ln["machine"] != self.host or p["machines"].get(self.host) != pub_b64(MKEYS[self.host]):
            return self.refuse("op_plan_line refused: not this machine's line")
        if ln.get("verbs") and not p.get("code_given"):
            return self.refuse("op_plan_line refused: a root line needs the code")
        got = json.loads(base64.b64decode(args[2])) if len(args) > 2 and args[2] else []
        by = {}
        for s in got:
            r = sp.verify_receipt(s, p, sha)
            by[r["step"]] = r
        for k in range(1, n):
            r = by.get(p["lines"][k - 1]["step"])
            if not r or sp.receipt_failed(r):
                return self.refuse(f"op_plan_line refused: line {n} runs only after line {k}")
        self.lines_seen.append((n, [by[s]["step"] for s in by]))
        if len(args) > 3:
            opened = [json.loads(SealedBox(XKEYS[self.host]).decrypt(base64.b64decode(c))) for c in args[3:]]
            self.creds_seen.append(opened)
            if any(c["nonce"] != p["nonce"] for c in opened):
                return self.refuse("op_plan_line refused: a credential for another run")
        key = (p["nonce"], n)
        rec = self.records.get(key)
        allowed = int(ln.get("attempts") or 1)
        if rec and rec["state"] == "running":
            return self.refuse("op_plan_line refused: an attempt is running")
        if rec and rec["state"] == "passed":
            return self.refuse(f"op_plan_line refused: line {n} ran already")
        attempt = (rec["attempt"] + 1) if rec else 1
        if attempt > allowed:
            return self.refuse(f"op_plan_line refused: line {n} ran already ({allowed} time(s))")
        outcomes = self.script.get(n, ["pass"])
        outcome = outcomes[min(attempt - 1, len(outcomes) - 1)]
        base_r = {"plan": p["nonce"], "plan_sha256": sha, "pipeline": p["pipeline"], "commit": p["commit"], "n": n,
                  "step": ln["step"], "machine": self.host, "op_sha256": ln["op_sha256"],
                  "params_sha256": hashlib.sha256(ln["params"].encode()).hexdigest(),
                  "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "attempt": attempt}
        kind = outcome.split(":")[0]
        ok = kind != "fail"
        r = {**base_r, "ok": ok and kind != "verifyfail"}
        if kind == "verifyfail":
            r["ok"], r["verify_error"] = True, "logs.check: Traceback found"
        if not ok:
            r["error"] = "the program exited 1"
        if ln["step"] == "build" and r["ok"] and not r.get("verify_error"):
            r["images"] = [{"image": "registry.example.com/acme/app", "digest": "sha256:" + "e" * 64,
                            "source": "https://git.example.com/acme/app", "commit": p["commit"]}]
        signed = self.sign(r)
        self.records[key] = {"attempt": attempt, "state": "failed" if sp.receipt_failed(r) else "passed"}
        self.receipts.setdefault(key, [])
        if outcome.endswith(":slow") or outcome.endswith(":relay"):
            self.deferred[key] = signed
            self.records[key]["state"] = "running"
            if outcome.endswith(":slow"):
                raise nac_mgmt.MgmtError(504, f"'{self.host}' took this job and had not answered")
            return {"exit": 1, "stdout": "", "stderr": "relay error: ConnectionClosed()"}
        self.receipts[key].append(signed)
        return {"exit": 0, "stdout": json.dumps({"receipt": signed, "ok": r["ok"], "error": r.get("error", "")}),
                "stderr": ""}

    def status(self, args):
        if self.status_timeouts:
            self.status_timeouts -= 1
            raise nac_mgmt.MgmtError(504, f"'{self.host}' took this job and had not answered")
        key = (args[0], int(args[1]))
        if key in self.deferred:
            signed = self.deferred.pop(key)
            self.receipts.setdefault(key, []).append(signed)
            r = json.loads(base64.b64decode(signed["payload"]))
            self.records[key]["state"] = "failed" if sp.receipt_failed(r) else "passed"
        if key in self.deferred_undo:
            self.receipts.setdefault(key, []).append(self.deferred_undo.pop(key))
        return {"exit": 0, "stdout": json.dumps({"receipts": self.receipts.get(key, [])}), "stderr": ""}

    def undo(self, args):
        """ops_plan_linux.go planUndoLine + planUndoAllowed, rule for rule."""
        if self.undo_no_worker:
            self.undo_no_worker -= 1
            raise nac_mgmt.MgmtError(502, "no management worker connected")
        p, sha = self.open_plan(args[0])
        k = int(args[1])
        ln = p["lines"][k - 1]
        if ln["machine"] != self.host:
            return self.refuse("op_plan_undo refused: not this machine's line")
        if not ln.get("undo"):
            return self.refuse(f"op_plan_undo refused: line {k} is not one the plan lets be undone")
        fr = sp.verify_receipt(json.loads(base64.b64decode(args[2])), p, sha)
        later = fr["n"] > k and bool(fr.get("ok")) and bool(fr.get("verify_error"))
        own = fr["n"] == k and sp.receipt_failed(fr)
        if fr.get("undo") or not (later or own):
            return self.refuse(f"op_plan_undo refused: an undo of line {k} needs a signed receipt of a LATER line "
                               f"whose check failed, or of line {k}'s own failure")
        if later and int(fr.get("attempt") or 0) != int(p["lines"][fr["n"] - 1].get("attempts") or 1):
            return self.refuse("op_plan_undo refused: only its last attempt's failure undoes an earlier line")
        rec = self.records.get((p["nonce"], k))
        if not rec:
            return self.refuse(f"op_plan_undo refused: no record of running line {k}")
        if rec["state"] == "running":
            return self.refuse(f"op_plan_undo refused: an attempt of line {k} is running")
        if rec["state"] == "undone":
            return self.refuse(f"op_plan_undo refused: line {k} was undone here already")
        if later and rec["state"] != "passed":
            return self.refuse(f"op_plan_undo refused: line {k} did not pass here")
        if not later and rec["state"] == "passed":
            return self.refuse(f"op_plan_undo refused: an attempt of line {k} passed here")
        if not later and int(fr.get("attempt") or 0) != rec["attempt"]:
            return self.refuse(f"op_plan_undo refused: not the latest attempt of line {k}")
        rec["state"] = "undone"
        r = {"plan": p["nonce"], "plan_sha256": sha, "pipeline": p["pipeline"], "commit": p["commit"], "n": k,
             "step": ln["step"], "machine": self.host, "op_sha256": ln["op_sha256"], "params_sha256": "",
             "at": "2026-09-30T20:00:00Z", "ok": True, "undo": True}
        signed = self.sign(r)
        if self.undo_slow:
            self.undo_slow = False
            self.deferred_undo[(p["nonce"], k)] = signed
            raise nac_mgmt.MgmtError(504, f"'{self.host}' took this job and had not answered")
        self.receipts[(p["nonce"], k)].append(signed)
        return {"exit": 0, "stdout": json.dumps({"receipt": signed, "ok": True}), "stderr": ""}


BOXES: dict = {}
DENY: set = set()
AUDIT: list = []


async def fake_run_mgmt_op(pool, eid, server, op, args, *, token_id=None, note="", approval_wait_s=None,
                           origin="service", budget_s=None):
    AUDIT.append((server, op, budget_s))
    if (server, op) in DENY:
        return {"server": server, "op": op, "exit": 126, "stdout": "", "stderr": "not permitted — no signed grant"}
    box = BOXES[server]
    return {"op_plan_line": box.line, "op_plan_status": box.status, "op_plan_undo": box.undo}[op](
        [str(a) for a in args])


RACE = {"barrier": None}


async def fake_gate_op(pool, eid, server, op):
    if RACE["barrier"] is not None and op == "op_plan_line":
        # Both racers reach the gate before either goes on: a real race.
        await RACE["barrier"].wait()
    if (server, op) in DENY:
        return False, "no signed grant (default-deny)", "denied-pod"
    return True, "", "ran"


async def fake_box(pool, token_id, server, op, args, budget):
    assert op == "op_sync"
    BOXES[server].synced.append(json.loads(base64.b64decode(args[0]))["sha256"])
    return 0, "{}", ""


async def fake_sign(pool, eid, terms):
    return b64(json.dumps(terms).encode()), "sig"


async def fake_box_gate(pool, server, token_id=None):
    return {"write_gate": True, "device": True, "device_keys": [DEVICE_PUB], "anchored": True, "is_anchor": True,
            "action_ops": True, "action_ops_reason": "", "encryption_key": enc_claim(server), "seen": True,
            "second_factor": True, "stepup": True, "sandbox": True, "sandbox_reason": ""}


async def fake_find_device(pool, eid, keys):
    return {"token_id": 99, "hostname": "device-box"} if DEVICE_PUB in keys else None


class Device:
    def __init__(self):
        self.asks = {}
        self.answer = "pending"
        self.offline = False
        self.tamper = False
        self.handed = set()

    async def file(self, pool, eid, device, details, server):
        if self.offline:
            return {"state": "offline", "detail": "your approval device (device-box) is not connected right now"}
        rid = f"ask-{len(self.asks) + 1}"
        self.asks[rid] = details
        return {"state": "pending", "id": rid}

    def sign(self, draft):
        root = any(ln.get("verbs") for ln in draft["lines"])
        lines = copy.deepcopy(draft["lines"])
        if self.tamper:
            lines[0]["params"] = '{"commit":"' + "f" * 40 + '"}'
        p = {"v": 1, "kind": "ship_plan", "pipeline": draft["pipeline"], "version": 1, "commit": draft["commit"],
             "nonce": secrets.token_hex(16), "expires": draft["expires"], "root": root, "code_given": root,
             "device": DEVICE_PUB, "machines": {h: pub_b64(MKEYS[h]) for h in HOSTS}, "lines": lines}
        return json.dumps(dsse(DEVICE, sp.PLAN_PAYLOAD_TYPE, json.dumps(p).encode()))

    async def status(self, pool, eid, device, rid):
        if self.answer == "approved":
            if rid in self.handed:
                return {"state": "expired", "detail": "the request lapsed on the device"}
            self.handed.add(rid)
            return {"state": "approved", "approval": self.sign(self.asks[rid]["plan"])}
        if self.answer == "denied":
            return {"state": "denied", "detail": "you refused this", "by_person": True}
        if self.answer == "expired":
            return {"state": "expired", "detail": "the request lapsed on the device"}
        return {"state": "pending", "id": rid}


DEV = Device()
WAKES: list = []


async def fake_schedule(pool, eid, type_row, title, prompt):
    WAKES.append((str(type_row["id"]), title, prompt))
    return {"status": "ok"}


# ── the repository: ops and the pipeline on disk ────────────────────────────

TMP = Path(tempfile.mkdtemp(prefix="ships-runner-", dir=os.environ.get("SHIPS_TMP") or None))
project_modules.PROJECTS_ROOT = TMP
MOD = TMP / "1" / "shipmod"
CREDS = "credentials: [\"registry:acme\"]\n" if HAVE_NACL else ""
BUILD = f"""name: app-build
version: 1.0.0
summary: Build and push the app image.
kind: action
target: machine
privilege: user
interpreter: /bin/sh
time_limit: 600
params:
  commit: {{type: string, max_len: 64}}
destinations: ["git.example.com:443"]
{CREDS}modes: [dry_run, apply, verify]
undo: none
"""
ROLL = """name: app-roll
version: 1.0.0
summary: Roll the app to the built digest.
kind: action
target: machine
privilege: root
interpreter: /bin/sh
time_limit: 60
params:
  digest: {type: string, max_len: 71}
verbs:
  - {verb: k8s.set_image, namespace: apps, kind: deployment, name: app, container: app, image: registry.example.com/acme/app, digest: "{{digest}}", repo: "https://git.example.com/acme/app"}
modes: [dry_run, apply, verify]
undo: none
"""


def write_op(name, text):
    d = MOD / "ops" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "op.yaml").write_text(text)
    (d / "run").write_text("#!/bin/sh\ntrue\n")


def write_pipeline(build_rule="stop", roll_rule="stop", roll_undo=False, build_undo=False, turn_type="ships",
                   name="web"):
    d = MOD / "ships" / name
    d.mkdir(parents=True, exist_ok=True)
    creds = '    credentials: ["registry:acme"]\n' if HAVE_NACL else ""
    (d / "pipeline.yaml").write_text(f"""format: privatae.ship/v1
name: {name}
repo: https://git.example.com/acme/app.git
branch: main
mode: automated
turn_type: {turn_type}
steps:
  - step: build
    machine: build-box
    op: app-build
    params: {{commit: "{{{{commit}}}}"}}
{creds}    undo: {str(build_undo).lower()}
    on_fail: {build_rule}
  - step: roll
    machine: k8s-box
    op: app-roll
    inputs: {{digest: build.digest}}
    undo: {str(roll_undo).lower()}
    on_fail: {roll_rule}
""")


write_op("app-build", BUILD)
write_op("app-roll", ROLL)

# The ship tables (v194), emptied between scenarios — children first.
SHIP_TABLES = ("ship_steps", "ship_runs", "ship_pipelines", "ship_machine_keys")
TOKENS = list(HOSTS.values())


async def setup_once(pool):
    """What every scenario shares, in the gate's own clone: the project the
    op module lives in, and one ACTIVE turn type named 'ships', drafted and
    activated through turn_type_approval (the one writer of a type;
    test_turn_types_approval sweeps for any other)."""
    from mastercode.runtime import turn_type_approval as tta
    await pool.execute("INSERT INTO projects (id, entity_id, name) VALUES (1, $1, 'ships gate')", EID)
    asked = await tta.draft(pool, EID, "ships", {"keywords": ["ship"]}, reason="the ships gate")
    assert asked.get("status") == "ok", asked
    tid = asked["result"]["ticket_id"]
    meta = await pool.fetchval("SELECT metadata FROM entity_tickets WHERE id = $1", tid)
    meta = json.loads(meta) if isinstance(meta, str) else meta
    done = await tta.apply_proposal(pool, EID, meta, option_id=tta.OPT_ACTIVATE, ticket_id=tid)
    assert done.get("applied"), done


async def reset(pool):
    for ship_table in SHIP_TABLES:
        await pool.execute(f"DELETE FROM {ship_table}")
    await pool.execute("DELETE FROM network_bastion_heartbeat WHERE token_id = ANY($1::int[])", TOKENS)
    await pool.execute("DELETE FROM mcp_client_tokens WHERE id = ANY($1::int[])", TOKENS)
    await pool.execute("DELETE FROM project_modules WHERE project_id = 1")
    for h, t in HOSTS.items():
        # token_hash and url_handle are unique among live tokens: each its own.
        await pool.execute(
            "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
            "VALUES ($1, $2, 'ships gate', decode(md5($3), 'hex'), $3, 'bastion')",
            t, EID, f"ships-gate-{t}")
        await pool.execute("INSERT INTO network_bastion_heartbeat (token_id, hostname, gateway_version) "
                           "VALUES ($1,$2,'0.47.0')", t, h)
    await pool.execute("INSERT INTO project_modules (project_id, name, target, validation) "
                       "VALUES (1, 'shipmod', 'op', 'checks')")
    BOXES.clear()
    BOXES.update({h: Box(h) for h in HOSTS})
    DENY.clear()
    AUDIT.clear()
    WAKES.clear()
    DEV.__init__()
    VAULT.clear()
    VAULT["registry:acme"] = b"push-token"
    runner._locks.clear()


VAULT = {"registry:acme": b"push-token"}


async def vault_open(pool, *, entity_id, name, actor):
    return VAULT.get(name)


async def vault_list(pool, *, entity_id):
    return [{"logical_name": k, "deactivated_at": None} for k in VAULT]


def patch():
    nac_mgmt.run_mgmt_op = fake_run_mgmt_op
    nac_mgmt.gate_op = fake_gate_op
    nac_ops._box = fake_box
    nac_ops._sign = fake_sign
    nac_approval.box_gate = fake_box_gate
    nac_approval.find_device = fake_find_device
    nac_approval._device_file = DEV.file
    nac_approval._device_status = DEV.status
    runner._schedule = fake_schedule
    from server.vault import api as vault_api
    vault_api.open_secret = vault_open
    vault_api.list_secrets = vault_list


COMMIT = "c" * 40


async def run_to_rest(pool, rid, ticks=6):
    for _ in range(ticks):
        await runner.tick(pool, EID)
    return await runner.run_view(pool, rid)


async def started(pool, **pipe):
    write_pipeline(**pipe)
    v = await runner.start_run(pool, EID, "web", COMMIT, by="alex")
    return v["id"], v


async def main(dsn):
    patch()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    await setup_once(pool)

    print("a clean ship")
    await reset(pool)
    rid, v = await started(pool)
    check("start files one ask on the device with the whole draft", v["state"] == "awaiting_tap"
          and len(DEV.asks) == 1 and len(DEV.asks["ask-1"]["plan"]["lines"]) == 2
          and DEV.asks["ask-1"]["kind"] == "ship_plan")
    v = await run_to_rest(pool, rid, 2)
    check("nothing moves before the tap", v["state"] == "awaiting_tap" and not AUDIT)
    DEV.answer = "approved"
    v = await run_to_rest(pool, rid)
    check("after the tap the run passes, both lines on their machines", v["state"] == "passed",
          f"{v['state']} {v['error']}")
    check("each machine was sent only its own line", [x[1] for x in AUDIT if x[1] == "op_plan_line"] ==
          ["op_plan_line", "op_plan_line"] and [x[0] for x in AUDIT if x[1] == "op_plan_line"] == ["build-box", "k8s-box"])
    check("line 2 came with line 1's receipt", BOXES["k8s-box"].lines_seen == [(2, ["build"])])
    check("each op was synced to its machine before its line", len(BOXES["build-box"].synced) == 1
          and len(BOXES["k8s-box"].synced) == 1)
    check("the dispatch waits at most the runner's wait", all(b == runner.DISPATCH_WAIT_S for s, o, b in AUDIT
                                                               if o == "op_plan_line"))
    check("each step keeps its verified receipt", all(s["receipt"] and s["receipt"]["ok"] for s in v["steps"])
          and v["steps"][0]["receipt"]["images"][0]["digest"].startswith("sha256:"))
    check("the roll is shown as root work with its verb", v["steps"][1]["root"] and
          v["steps"][1]["verbs"][0]["verb"] == "k8s.set_image")
    if HAVE_NACL:
        check("the build's credential was sealed to its machine for this run and opened there",
              BOXES["build-box"].creds_seen and BOXES["build-box"].creds_seen[0][0]["name"] == "registry:acme"
              and BOXES["build-box"].creds_seen[0][0]["value"] == "push-token")
        pin = await pool.fetchrow("SELECT encryption_key FROM ship_machine_keys WHERE token_id=11")
        check("the machine's encryption key was pinned on first use", pin is not None)
    else:
        print("  SKIPPED sealed credentials: PyNaCl is not installed for this python")

    print("one run at a time")
    await reset(pool)
    rid, _ = await started(pool)
    try:
        await runner.start_run(pool, EID, "web", COMMIT)
        second = None
    except runner.RunnerError as e:
        second = e
    check("a second run of the same pipeline is refused while one is under way",
          second is not None and second.status == 409 and "already under way" in second.detail)

    print("a long line and a dropped connection are read back")
    for kind in ("slow", "relay"):
        await reset(pool)
        rid, _ = await started(pool)
        DEV.answer = "approved"
        BOXES["build-box"].script = {1: [f"pass:{kind}"]}
        await runner.tick(pool, EID)
        v = await runner.run_view(pool, rid)
        check(f"{kind}: the line is 'sent', not failed", v["steps"][0]["state"] == "sent" and v["state"] == "running",
              f"{v['steps'][0]['state']} {v['state']} {v['error']}")
        v = await run_to_rest(pool, rid)
        check(f"{kind}: its receipt is read back and the ship passes", v["state"] == "passed", v["error"])

    print("retry")
    await reset(pool)
    rid, _ = await started(pool, build_rule="{rule: retry, times: 1}")
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["fail", "pass"]}
    v = await run_to_rest(pool, rid)
    check("a failed attempt runs again and the ship passes", v["state"] == "passed"
          and v["steps"][0]["attempts"] == 2 and v["steps"][0]["receipt"]["attempt"] == 2, f"{v['state']} {v['error']}")
    await reset(pool)
    rid, _ = await started(pool, build_rule="{rule: retry, times: 1}")
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["fail", "fail"]}
    v = await run_to_rest(pool, rid)
    check("retries spent: the run fails and says so", v["state"] == "failed" and "all the rule allows" in v["error"],
          v["error"])

    print("undo, by the machine's own rule")

    def strikes():
        return [r for b in BOXES.values() for r in b.refusals]

    await reset(pool)
    rid, _ = await started(pool, roll_rule="undo", roll_undo=True, build_undo=True)
    DEV.answer = "approved"
    BOXES["k8s-box"].script = {2: ["verifyfail"]}
    v = await run_to_rest(pool, rid, 10)
    check("a failed CHECK takes back the undoable steps, newest first", v["state"] == "undone"
          and [s["state"] for s in v["steps"]] == ["undone", "undone"], f"{v['state']} {v['error']}")
    undos = [(s, o) for s, o, _b in AUDIT if o == "op_plan_undo"]
    check("the undo went to the roll's machine first, then the build's", undos == [("k8s-box", "op_plan_undo"),
                                                                                    ("build-box", "op_plan_undo")])
    check("each undo keeps its receipt", all(s["undo_receipt"] and s["undo_receipt"]["undo"] for s in v["steps"]))
    check("no undo was refused (no strike)", not strikes(), strikes())

    await reset(pool)
    rid, _ = await started(pool, roll_rule="undo", roll_undo=True, build_undo=True)
    DEV.answer = "approved"
    BOXES["k8s-box"].script = {2: ["fail"]}
    v = await run_to_rest(pool, rid, 10)
    check("a failed APPLY takes back only the failed step, and says why the earlier one stays",
          v["state"] == "undone" and [s["state"] for s in v["steps"]] == ["passed", "undone"]
          and "step(s) 1 stay as they are" in v["error"] and "before its check ran" in v["error"], f"{v}")
    check("the earlier step was never sent an undo the machine would refuse (no strike)",
          not strikes() and [o for _s, o, _b in AUDIT].count("op_plan_undo") == 1, strikes())

    await reset(pool)
    rid, _ = await started(pool, roll_rule="undo", build_undo=True)
    DEV.answer = "approved"
    BOXES["k8s-box"].script = {2: ["fail"]}
    v = await run_to_rest(pool, rid, 10)
    check("a failed apply of a step that may not be undone: nothing is sent, and the run says why",
          v["state"] == "failed" and "no step may be undone" in v["error"] and "stay as they are" in v["error"]
          and not strikes() and "op_plan_undo" not in [o for _s, o, _b in AUDIT], v["error"])

    await reset(pool)
    rid, _ = await started(pool, roll_rule="undo")
    DEV.answer = "approved"
    BOXES["k8s-box"].script = {2: ["fail"]}
    v = await run_to_rest(pool, rid, 10)
    check("with nothing undoable the run fails and says so", v["state"] == "failed"
          and "no step may be undone" in v["error"], v["error"])

    await reset(pool)
    rid, _ = await started(pool, roll_rule="undo", roll_undo=True, build_undo=True)
    DEV.answer = "approved"
    BOXES["k8s-box"].script = {2: ["verifyfail"]}
    BOXES["k8s-box"].undo_no_worker = 1
    await runner.tick(pool, EID)
    v = await runner.run_view(pool, rid)
    check("an undo that reached no worker waits, it is not abandoned", v["state"] == "undoing"
          and v["steps"][1]["state"] == "failed", f"{v['state']} {[s['state'] for s in v['steps']]} {v['error']}")
    v = await run_to_rest(pool, rid, 10)
    check("...and goes out on a later tick", v["state"] == "undone", f"{v['state']} {v['error']}")

    await reset(pool)
    rid, _ = await started(pool, roll_rule="undo", roll_undo=True, build_undo=True)
    DEV.answer = "approved"
    BOXES["k8s-box"].script = {2: ["verifyfail"]}
    BOXES["k8s-box"].undo_slow = True
    await runner.tick(pool, EID)
    await runner.stop_run(pool, rid)
    v = await run_to_rest(pool, rid, 10)
    check("Stop during an undo: the undo on a machine is read back, no further undo is sent",
          v["state"] == "stopped" and [s["state"] for s in v["steps"]] == ["passed", "undone"]
          and "after step(s) 2 were taken back" in v["error"]
          and [s for s, o, _b in AUDIT if o == "op_plan_undo"] == ["k8s-box"], f"{v['state']} {v['error']}")

    print("stop and ask")
    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["fail"]}
    v = await run_to_rest(pool, rid)
    check("stop: the run fails at the step, naming it", v["state"] == "failed" and "step 1 (build)" in v["error"]
          and not WAKES, v["error"])
    await reset(pool)
    rid, _ = await started(pool, build_rule="ask")
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["fail"]}
    v = await run_to_rest(pool, rid)
    check("ask: the run waits for the Host, woken under the pipeline's turn type",
          v["state"] == "failed" and v["needs_host"] and v["host_woken"] and len(WAKES) == 1
          and "run " + str(rid) in WAKES[0][2], f"{v} {WAKES}")
    await reset(pool)
    rid, _ = await started(pool, build_rule="ask", turn_type="nothing-like-it")
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["fail"]}
    v = await run_to_rest(pool, rid)
    check("ask with no such active type: nothing is woken, and the run says so",
          v["needs_host"] and not v["host_woken"] and not WAKES and "No active turn type" in v["error"], v["error"])

    print("the device")
    for ans, want in (("denied", "you refused"), ("expired", "no longer holds")):
        await reset(pool)
        rid, _ = await started(pool)
        DEV.answer = ans
        v = await run_to_rest(pool, rid)
        check(f"{ans} on the device: the run stops and nothing is sent", v["state"] == "stopped"
              and want in v["error"] and not AUDIT, v["error"])
    await reset(pool)
    rid, _ = await started(pool)
    await pool.execute("UPDATE ship_runs SET plan_expires=now() - interval '1 minute' WHERE id=$1", rid)
    v = await run_to_rest(pool, rid)
    check("a plan that lapsed before the tap stops the run", v["state"] == "stopped" and "lapsed" in v["error"])
    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer, DEV.tamper = "approved", True
    v = await run_to_rest(pool, rid)
    check("a device answer that is not this run's plan stops it", v["state"] == "stopped"
          and "not this run's plan" in v["error"] and not AUDIT, v["error"])

    print("refusals")
    await reset(pool)
    DENY.add(("k8s-box", "op_plan_line"))
    write_pipeline()
    try:
        await runner.start_run(pool, EID, "web", COMMIT)
        err = None
    except runner.RunnerError as e:
        err = e
    rows = await pool.fetchval("SELECT count(*) FROM ship_runs")
    check("no grant: refused before any row or ask, naming the machine and the op",
          err is not None and "k8s-box" in err.detail and "op_plan_line" in err.detail and rows == 0 and not DEV.asks,
          getattr(err, "detail", ""))
    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer = "approved"
    BOXES["build-box"].refuse_next = "the plan names another key for this machine"
    v = await run_to_rest(pool, rid)
    check("a machine that refuses a line stops the run with its words", v["state"] == "stopped"
          and "another key" in v["error"], v["error"])
    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer = "approved"
    BOXES["build-box"].forge = True
    v = await run_to_rest(pool, rid)
    check("a receipt not signed by the plan's machine key stops the run", v["state"] == "stopped"
          and "does not verify" in v["error"], v["error"])
    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer = "approved"
    (MOD / "ops" / "app-roll" / "run").write_text("#!/bin/sh\necho changed\n")
    v = await run_to_rest(pool, rid)
    (MOD / "ops" / "app-roll" / "run").write_text("#!/bin/sh\ntrue\n")
    check("an op changed after approval is not run", v["state"] == "stopped" and "changed after" in v["error"]
          and v["steps"][0]["state"] == "passed", v["error"])

    print("stop")
    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["pass:slow"]}
    await runner.tick(pool, EID)
    await runner.stop_run(pool, rid)
    v = await run_to_rest(pool, rid)
    check("Stop between lines: the line on a machine is read back, the next is never sent",
          v["state"] == "stopped" and v["steps"][0]["state"] == "passed" and v["steps"][1]["state"] == "waiting"
          and not BOXES["k8s-box"].lines_seen, f"{v['state']} {[s['state'] for s in v['steps']]}")
    await reset(pool)
    rid, _ = await started(pool)
    v = await runner.stop_run(pool, rid)
    check("Stop before the tap stops at once", v["state"] == "stopped")


    await reset(pool)
    rid, _ = await started(pool, roll_rule="undo", roll_undo=True, build_undo=True)
    DEV.answer = "approved"
    BOXES["k8s-box"].script = {2: ["verifyfail:slow"]}
    await runner.tick(pool, EID)            # step 2 is on its machine
    await runner.stop_run(pool, rid)        # Stop pressed while it runs
    v = await run_to_rest(pool, rid, 10)
    check("a Stop pressed while a step ran does not cancel that step's rollback", v["state"] == "undone"
          and [s["state"] for s in v["steps"]] == ["undone", "undone"] and not strikes(),
          f"{v['state']} {v['error']} {strikes()}")

    await reset(pool)
    rid, _ = await started(pool, roll_rule="undo", roll_undo=True, build_undo=True)
    DEV.answer = "approved"
    BOXES["k8s-box"].script = {2: ["verifyfail"]}
    BOXES["k8s-box"].undo_no_worker = 1
    await runner.tick(pool, EID)            # the first undo reached no worker
    await pool.execute("UPDATE ship_runs SET plan_envelope=$2 WHERE id=$1", rid,
                       (await pool.fetchval("SELECT plan_envelope FROM ship_runs WHERE id=$1", rid)))
    runner_now = runner._now
    runner._now = lambda: runner_now() + timedelta(hours=5)
    try:
        v = await run_to_rest(pool, rid, 4)
    finally:
        runner._now = runner_now
    check("an undo that would run after the plan expired is not sent; the run says what was not taken back",
          v["state"] == "failed" and "expired during the undo" in v["error"] and not strikes()
          and [o for _s, o, _b in AUDIT].count("op_plan_undo") == 1, f"{v['state']} {v['error']} {strikes()}")

    print("reading back, budgets, one sender")
    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["pass:slow"]}
    await runner.tick(pool, EID)
    BOXES["build-box"].status_timeouts = 2
    await runner.tick(pool, EID)
    v = await runner.run_view(pool, rid)
    check("a status read that timed out (504) waits; it does not stop a healthy run",
          v["state"] == "running" and v["steps"][0]["state"] == "sent", f"{v['state']} {v['error']}")
    v = await run_to_rest(pool, rid)
    check("...and the receipt is read back once the machine answers", v["state"] == "passed", v["error"])
    budget = await pool.fetchval("SELECT stale_s FROM ship_steps WHERE run_id=$1 AND n=1", rid)
    check("a sent line's wait comes from its op (twice its time limit, six minutes a verb, a margin)",
          budget == 2 * 600 + runner.STALE_MARGIN_S, budget)
    budget2 = await pool.fetchval("SELECT stale_s FROM ship_steps WHERE run_id=$1 AND n=2", rid)
    check("...a verb adds six minutes", budget2 == 2 * 60 + runner.VERB_WAIT_S + runner.STALE_MARGIN_S, budget2)

    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["pass:slow"]}
    await runner.tick(pool, EID)   # takes the tap: running, step 1 sent
    await pool.execute("UPDATE ship_steps SET state='waiting', attempts=0, sent_at=NULL WHERE run_id=$1", rid)
    BOXES["build-box"].records.clear()
    BOXES["build-box"].deferred.clear()
    BOXES["build-box"].lines_seen.clear()
    BOXES["build-box"].script = {1: ["pass:slow"]}
    r1 = await runner._run(pool, rid)
    RACE["barrier"] = asyncio.Barrier(2)
    await asyncio.gather(runner._advance_once(pool, EID, r1), runner._advance_once(pool, EID, dict(r1)))
    RACE["barrier"] = None
    check("two ticks racing on one waiting line send it once", len(BOXES["build-box"].lines_seen) == 1
          and not strikes(), f"{BOXES['build-box'].lines_seen} {strikes()}")

    print("the person's no, and a device that cannot be reached")
    await reset(pool)
    rid, _ = await started(pool)
    DEV.answer = "denied"
    v = await run_to_rest(pool, rid)
    try:
        await runner.start_run(pool, EID, "web", COMMIT, by=runner.HOST)
        again = None
    except runner.RunnerError as e:
        again = e
    check("after a no on the device the Host may not ask again for 24h",
          again is not None and again.status == 409 and "24 hours" in again.detail, getattr(again, "detail", ""))
    write_pipeline(name="web-copy")
    try:
        await runner.start_run(pool, EID, "web-copy", COMMIT, by=runner.HOST)
        again = None
    except runner.RunnerError as e:
        again = e
    check("...nor under a renamed copy of the pipeline: the lock is on the machines",
          again is not None and again.status == 409 and "build-box" in again.detail and len(DEV.asks) == 1,
          getattr(again, "detail", ""))
    DEV.answer = "pending"
    v2 = await runner.start_run(pool, EID, "web", COMMIT, by="you, in the Ships app")
    check("...the person may start it themselves", v2["state"] == "awaiting_tap")
    await reset(pool)
    rid, _ = await started(pool)
    await runner.stop_run(pool, rid)
    try:
        await runner.start_run(pool, EID, "web", COMMIT, by=runner.HOST)
        again = None
    except runner.RunnerError as e:
        again = e
    check("Stop before the tap counts as a no for the Host's next ask", again is not None and again.status == 409)

    await reset(pool)
    write_pipeline()
    DEV.offline = True
    v = await runner.start_run(pool, EID, "web", COMMIT, by="alex")
    rid = v["id"]
    check("a device that cannot be reached leaves the run planned, saying why", v["state"] == "planned"
          and "not connected" in (v["error"] or ""), v)
    await pool.execute("UPDATE ship_runs SET plan_expires=now() - interval '1 minute' WHERE id=$1", rid)
    DEV.offline = False
    v = await run_to_rest(pool, rid)
    check("a planned run whose plan lapsed is stopped, and the device is never rung for it",
          v["state"] == "stopped" and "lapsed" in v["error"] and not DEV.asks, f"{v['state']} {v['error']} {DEV.asks}")

    if HAVE_NACL:
        print("credentials, before the tap")
        await reset(pool)
        VAULT.clear()
        write_pipeline()
        try:
            await runner.start_run(pool, EID, "web", COMMIT)
            err = None
        except runner.RunnerError as e:
            err = e
        check("a credential missing from the vault is named before any ask",
              err is not None and "registry:acme" in err.detail and not DEV.asks, getattr(err, "detail", ""))

    print("the card, on the real tables")
    from mastercode import live_context as lc
    await reset(pool)
    rid, _ = await started(pool, build_rule="ask")
    DEV.answer = "approved"
    BOXES["build-box"].script = {1: ["fail"]}
    await run_to_rest(pool, rid)
    lines, _asof = await lc._fetch_ships(pool, EID)
    check("a run that stopped for the Host is on the card", any("NEEDS YOU: pipeline web run" in ln for ln in lines),
          lines)
    BOXES["build-box"].script = {}
    v = await runner.start_run(pool, EID, "web", COMMIT)
    await run_to_rest(pool, v["id"])
    lines, _asof = await lc._fetch_ships(pool, EID)
    left = await pool.fetchval("SELECT count(*) FROM ship_runs WHERE needs_host")
    check("a later run that passed clears it", not any("NEEDS YOU" in ln for ln in lines) and left == 0
          and any(f"run {v['id']}" in ln and "passed" in ln for ln in lines), lines)

    await pool.close()


sys.path.insert(0, str(ROOT / "scripts"))
from kernel_schema import kernel_schema_db  # noqa: E402

with kernel_schema_db(SERVER, entity_id=EID) as _dsn:
    asyncio.run(main(_dsn))
if failures:
    print(f"\nFAIL: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("\nOK: all checks passed" + ("" if HAVE_NACL else " (sealed credentials SKIPPED: no PyNaCl here)"))
